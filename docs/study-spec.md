# StudySpec v1

StudySpec is the JSON configuration consumed by `pineforge-hpo run`. The
implemented executable subset is intentionally narrow: `single_strategy`, one
strategy, one referenced OHLCV dataset, an expression objective, and grid,
seeded-random, adaptive dlib global, or native TPE sampling.

All strategy, artifact, and OHLCV paths are resolved relative to the StudySpec
file, not the caller's current working directory.

## Complete executable example

```json
{
  "schema_version": 1,
  "mode": "single_strategy",
  "strategies": [
    {
      "id": "trend",
      "source": "strategies/trend.pine",
      "datasets": ["ethusdt-1h"],
      "fixed_inputs": {
        "Direction": "long"
      },
      "strategy_overrides": {
        "initial_capital": 100000,
        "commission_value": 0.04
      },
      "search_space": {
        "Fast": {
          "kind": "integer",
          "low": 2,
          "high": 50,
          "step": 1
        },
        "Slow": {
          "kind": "integer",
          "low": 10,
          "high": 200,
          "step": 5
        },
        "Multiplier": {
          "kind": "real",
          "low": 0.5,
          "high": 2.0,
          "step": 0.1
        },
        "Use Filter": {
          "kind": "boolean"
        },
        "Mode": {
          "kind": "categorical",
          "choices": ["ema", "sma"]
        }
      }
    }
  ],
  "datasets": [
    {
      "id": "ethusdt-1h",
      "ohlcv": "data/ETHUSDT-1h.csv",
      "input_tf": "60",
      "script_tf": "60",
      "chart_timezone": "UTC"
    }
  ],
  "objective": {
    "kind": "expression",
    "direction": "maximize",
    "expression": "metrics.all.net_profit_pct - abs(metrics.equity.max_equity_drawdown_pct)",
    "constraints": [
      "metrics.all.num_trades >= 30",
      "metrics.equity.max_equity_drawdown_pct <= 20"
    ],
    "nan_policy": "fail_trial",
    "division_by_zero": "fail_trial"
  },
  "sampler": {
    "kind": "random",
    "candidate_policy": "sampler_default",
    "seed": 20260718,
    "trials": 1000
  },
  "execution": {
    "workers": 8,
    "isolation": "threads"
  }
}
```

Run it with:

```bash
pineforge-hpo run path/to/study.json \
  --engine-root external/pineforge-engine \
  --native ./build/bin/pineforge-hpo-native
```

## Top-level fields

| Field | Executable requirement |
| --- | --- |
| `schema_version` | Must be `1`. |
| `mode` | Must be `"single_strategy"`. |
| `strategies` | Must contain exactly one strategy object. |
| `datasets` | The executable path requires exactly one referenced dataset. |
| `objective` | Must use `kind="expression"`. |
| `sampler` | `grid`, `random`, `dlib_global`, or `tpe`, plus its candidate policy. |
| `execution` | Sequential or threaded native execution. |

Unknown fields, duplicate JSON keys, and JSON `NaN`/`Infinity` constants are
rejected.

## Strategy

A strategy defines exactly one of:

```json
{"source": "strategy.pine"}
```

or:

```json
{"artifact": "cache/or/exported/strategy.dylib"}
```

`source` uses the direct PineScript path:

```text
pineforge-codegen-oss / pineforge_codegen.transpile_full()
  -> generated.cpp
  -> cached native plugin
```

`artifact` skips transpilation and compilation and is useful for an already
validated plugin. It must remain beside its ArtifactBuilder `manifest.json`;
the runner verifies the plugin hash and uses the manifest to validate every
input name, type, bound, and option before execution. The Python distribution
is called `pineforge-codegen`; the repository is `pineforge-codegen-oss`; the
module is `pineforge_codegen`.

Other strategy fields:

- `id`: non-empty identifier included in provenance;
- `datasets`: non-empty list of dataset IDs; the executable currently requires
  one;
- `fixed_inputs`: input titles and finite JSON scalar values applied on every
  trial;
- `strategy_overrides`: fixed runtime overrides applied on every trial;
- `search_space`: non-empty map of tunable Pine input titles.

An input cannot appear in both `fixed_inputs` and `search_space`.

### Runtime overrides

Candidate values and `fixed_inputs` are serialized and passed to
`strategy_set_input()`. `strategy_overrides` are passed to
`strategy_set_override()` before execution. This allows supported engine fields
such as initial capital, commission, pyramiding, slippage, and quantity settings
to change per study without retranspiling the Pine source.

The engine validates the final string representation. Invalid values produce a
trial error; they must not silently fall back to Pine defaults.

## Search dimensions

### Integer

```json
{"kind": "integer", "low": 2, "high": 50, "step": 1}
```

`low` and `high` are inclusive integers. `step` must be positive.

Log-scaled integer, usable by every implemented sampler:

```json
{"kind": "integer", "low": 1, "high": 1000000, "log": true}
```

Log bounds must be strictly positive and `step` must be `1` (its default).
Random, dlib global, and TPE use the transformed sampling interval
`[ln(low - 0.5), ln(high + 0.5)]`, retain observations at `ln(value)`, and
decode by rounding `exp(z)` to the nearest in-range integer. Grid still
enumerates every integer in declaration order; `log` changes model geometry,
not grid cardinality.

### Real

Stepped real, usable by all implemented samplers:

```json
{"kind": "real", "low": 0.5, "high": 3.0, "step": 0.1}
```

Continuous real, usable by random, dlib global, and TPE search:

```json
{"kind": "real", "low": 0.5, "high": 3.0}
```

`sampler_default` preserves this continuous form for random, dlib global, and
TPE. Grid and both finite candidate policies require `step` whenever
`low != high`. A fixed real with `low == high` contributes one value and does
not require `step`.

Log-scaled real, usable by random, dlib global, and TPE search:

```json
{"kind": "real", "low": 1e-6, "high": 1000, "log": true}
```

Both bounds must be strictly positive. A log real cannot declare `step`, so a
non-constant log real is not a finite grid dimension and is rejected by
`without_replacement` and `exhaustive`. A fixed log real remains a valid
one-value finite dimension. Sampled values and native output remain in the
original units even though adaptive models use `z = ln(value)` internally.

### Boolean

```json
{"kind": "boolean"}
```

Grid order is `false`, then `true`.

### Categorical

```json
{"kind": "categorical", "choices": ["ema", "sma", "wma"]}
```

`choices` must contain at least one finite JSON scalar. Choice order is
significant for deterministic grid enumeration. String, integer, real, and
boolean choice types are preserved in native trial JSON and must match the
compiled Pine input type.

Conditional search spaces are not implemented. `log` is valid only on integer
and real dimensions under the contracts above.

### Finite-domain cardinality

`without_replacement` and `exhaustive` require an exact finite Cartesian
domain. The legal values of an integer or varying stepped-real dimension are

```text
{ low + k * step | k is a non-negative integer and low + k * step <= high }
```

`high` is an inclusive upper bound but belongs to the lattice only when some
integer `k` reaches it. For example, `low=0`, `high=1`, and `step=0.3` yield
`0`, `0.3`, `0.6`, and `0.9`, not an extra off-step endpoint. Fixed real
dimensions contribute one value, booleans contribute two, and categorical
dimensions contribute their declared number of choices. The domain cardinality
is the checked product of these per-dimension counts; overflow or an undefined
count is an initialization error for a finite policy.

The binary64 codec evaluates a real grid point with one fused multiply-add. It
snaps only a final decoded value no more than one representable value above
`high`; this makes decimal `0..0.3 step 0.1` contain the declared `0.3` while
leaving `0..1 step 0.3` off-lattice. A stepped real may contain at most `2^53`
ordinals. Construction rejects any real or categorical grid whose distinct
ordinals serialize to the same strategy-ABI value.

Uniqueness applies to the complete parameter vector, not to individual fields.
The coordinator reserves the vector before returning it to a worker, so pending
requests participate in duplicate prevention.

## Dataset

```json
{
  "id": "ethusdt-1h",
  "ohlcv": "data/ETHUSDT-1h.csv",
  "input_tf": "60",
  "script_tf": "60",
  "chart_timezone": "UTC"
}
```

The current run command accepts one dataset. The CSV loader expects normalized
OHLCV rows with timestamps compatible with PineForge's public bar contract.

- `id`: unique non-empty ID referenced by the strategy;
- `ohlcv`: CSV path relative to the StudySpec;
- `input_tf`: chart/input timeframe passed to `run_backtest_full()`;
- `script_tf`: script timeframe; defaults to `input_tf` in the loader;
- `chart_timezone`: chart timezone; defaults to `UTC`.

Multi-dataset train/validation/test roles and walk-forward folds are not
executable yet.

## Objective

The executable objective is an expression:

```json
{
  "kind": "expression",
  "direction": "maximize",
  "expression": "metrics.all.net_profit_pct / max(1, metrics.equity.max_equity_drawdown_pct)",
  "constraints": ["metrics.all.num_trades >= 30"],
  "nan_policy": "fail_trial",
  "division_by_zero": "fail_trial"
}
```

`direction` is `maximize` or `minimize`.

Supported expression syntax:

- finite numeric literals;
- metric identifiers;
- `+`, `-`, `*`, `/`, unary `+`, unary `-`;
- parentheses;
- `min(a, b)`, `max(a, b)`, `abs(a)`;
- `<`, `<=`, `>`, `>=`, `==`, `!=`.

Comparisons evaluate to `1.0` or `0.0`. Constraint expressions are satisfied
when non-zero.

Metric lookup supports:

- `metrics.all.<trade-stat>`;
- `metrics.longs.<trade-stat>`;
- `metrics.shorts.<trade-stat>`;
- `metrics.equity.<equity-stat>`;
- `report.total_trades`, `report.net_profit`, `report.input_bars_processed`,
  `report.script_bars_processed`, `report.magnifier_sample_ticks_total`, and
  their short aliases (without the `report.` prefix).

`<trade-stat>` and `<equity-stat>` are the field names of the engine's
`pf_trade_stats_t` and `pf_equity_stats_t`. Engine 1.0 renamed two equity
fields, and both spellings are accepted:

| Name | Pre-1.0 alias | Value |
| --- | --- | --- |
| `metrics.equity.sharpe_monthly` | `metrics.equity.sharpe_tv` | Sharpe ratio of month-end equity returns (chart timezone), 2%/yr risk-free rate, sample standard deviation, annualized by √12 |
| `metrics.equity.sortino_monthly` | `metrics.equity.sortino_tv` | Sortino ratio of the same monthly returns, population downside deviation against the same risk-free rate, annualized by √12 |

Both are NaN with fewer than two monthly returns or zero deviation. For Sortino,
zero downside deviation means no monthly return fell below the risk-free rate, so
the strongest trials can be NaN. Under the default `nan_policy` (`fail_trial`) a NaN
metric fails the trial.

Use the 1.0 names in new studies. An alias resolves to the same value, so a
StudySpec written for an earlier engine runs unchanged; the aliases are also the
keys of these two ratios in the engine's JSON report. A result lists each metric
under the name the expression used.

Missing/non-finite metrics and division by zero follow the configured reject
policy and leave the trial auditable as an objective or constraint error. An
unknown metric fails before the first backtest. Constraints always reject
division by zero and non-finite operands; a non-finite final objective is never
feasible or rankable even when non-finite intermediate objective evaluation is
enabled.

The JSON loader recognizes a future `kind="registered"` shape, and the C++
library exposes `ObjectiveFn<Observation>` for custom functions. The executable
CLI does not resolve registered objective names yet.

## Sampler

Every sampler requires an explicit non-negative `seed` and positive `trials`.
The optional `candidate_policy` defaults to `sampler_default`. Only `tpe`
accepts a non-empty `config`; other sampler kinds reject it instead of silently
ignoring configuration.

### Candidate policy

| Value | Supported samplers | Validation and behavior |
| --- | --- | --- |
| `sampler_default` | All | Preserve native sampler behavior. Continuous real dimensions and repeated adaptive proposals remain possible. |
| `without_replacement` | `tpe`, `grid` | Require a finite domain, reserve every complete vector at most once, and require `trials <= domain cardinality`. |
| `exhaustive` | `tpe`, `grid` | Require a finite domain and `trials == domain cardinality`; every declared complete vector is proposed exactly once. |

`candidate_policy` is a sibling of `kind`, `seed`, `trials`, and `config`:

```json
{
  "kind": "tpe",
  "candidate_policy": "without_replacement",
  "seed": 20260718,
  "trials": 1000,
  "config": {
    "startup_trials": 100
  }
}
```

The finite policies reject `random` and `dlib_global`; they never silently
change those samplers into grid or TPE. `sampler_default` is backward compatible
with existing continuous TPE studies.

### Grid

```json
{"kind": "grid", "seed": 20260718, "trials": 1000}
```

Grid search enumerates the Cartesian product in stable order. `trials` caps the
number executed; a sufficiently large value exhausts the grid. `seed` is
retained for a uniform reproducibility/provenance shape but does not alter grid
order. `without_replacement` makes the already-unique grid contract explicit
and requires the cap not to exceed cardinality. `exhaustive` requires it to
equal cardinality.

### Random

```json
{"kind": "random", "seed": 20260718, "trials": 1000}
```

Random search requires an explicit non-negative seed and a positive trial count.
The native sampler uses a deterministic `mt19937_64` sequence and avoids
implementation-specific standard distributions. It supports only
`candidate_policy="sampler_default"`.

### dlib global

```json
{"kind": "dlib_global", "seed": 20260718, "trials": 1000}
```

`dlib_global` wraps dlib's `global_function_search` through an adaptive ask/tell
interface. The seed must be between `0` and `2147483647`, inclusive. A fixed
logical batch is proposed and reported in trial-id order with the selected fixed
feedback lag. The exact deterministic sequence is scoped to the same seed,
batch size/lag, dlib version, and native toolchain, independently of worker count.

`dlib_global` supports only `candidate_policy="sampler_default"`.

Integer and stepped-real dimensions use integer indices; booleans use `0/1`;
categorical choices use their declaration-order index. A one-value dimension is
removed from dlib's vector and restored in the candidate. Categorical indices
impose an artificial ordinal geometry, so this is not equivalent to categorical
TPE. Duplicate parameter sets may be proposed and are evaluated as separate
trials.

dlib always maximizes internally; minimize objectives are sign-inverted.
Failed and infeasible requests are abandoned because dlib has no native hard-
constraint contract. Only genuine feasible objective values train the model or
participate in reported best-trial selection. A future constraint-aware sampler
may add explicit violation magnitudes instead of fabricated objective scores.

### TPE

```json
{
  "kind": "tpe",
  "seed": 20260718,
  "trials": 1000,
  "config": {
    "startup_trials": 10,
    "ei_candidates": 24,
    "gamma_fraction": 0.1,
    "gamma_cap": 25,
    "prior_weight": 1.0,
    "constant_liar": true
  }
}
```

`tpe` is a native, single-objective Tree-structured Parzen Estimator sampler.
It fits independent per-dimension marginals to good (`l`) and bad (`g`)
completed observations. Numeric dimensions use bounded Gaussian mixtures;
integer and stepped-real values use quantized bin-mass likelihoods. Boolean and
categorical dimensions use prior-smoothed categorical probabilities. It draws
`ei_candidates` joint candidates from `l` and selects the largest summed
`log(l / g)` score.

The good-set target is:

```text
min(gamma_cap, max(1, ceil(gamma_fraction * completed_trials)))
```

and is clamped to leave at least one bad observation when the history permits.
Before `startup_trials` usable observations, proposals use the seeded random
path. Failed and constraint-violating trials are abandoned and train neither
estimator.

Under `sampler_default`, TPE may propose the same complete vector more than
once. Under either finite policy, every vector is atomically reserved before it
is returned by `ask()`. Pending, completed, infeasible, and failed vectors stay
in the seen set. An abandoned point remains ineligible for a later proposal even
though it does not enter either density estimator.

Because expression constraints currently return only pass/fail, TPE does not
learn the direction or magnitude of a violation. Studies with a tiny feasible
region should encode validity into the search space where possible; otherwise
the sampler may revisit infeasible regions. No fabricated penalty objective is
inserted automatically.

TPE config fields are strict; unknown fields are rejected:

| Field | Default | Requirement |
| --- | ---: | --- |
| `startup_trials` | `10` | Positive integer. |
| `ei_candidates` | `24` | Integer in `[1, 1000000]`; the upper bound prevents an accidental near-infinite `ask()`. |
| `gamma_fraction` | `0.1` | Finite number in `(0, 1]`. |
| `gamma_cap` | `25` | Positive integer. |
| `prior_weight` | `1.0` | Finite number greater than zero. |
| `constant_liar` | `true` | Boolean. |

`candidate_policy` is not a TPE estimator setting and therefore belongs beside
`config`, not inside it.

When `constant_liar=true`, parameters from outstanding requests are inserted
only into the bad estimator. This discourages duplicate concurrent proposals
without fabricating an objective value. The scheduler still reports completed
results in trial-id order, so reproducibility is scoped to the same seed,
batch size/lag, pruning policy, sampler config, and native build.

TPE retains and refits the complete usable history. It is intended for bounded
adaptive refinement, not `10^6`-`10^8` trial enumeration; use an external partitioned
stateless search to reduce a mega-scale domain before starting a TPE study.

CMA-ES, evolutionary sampling, durable study storage,
and multi-objective Pareto optimization are not currently available.

## Execution

Threaded execution:

```json
{"workers": 8, "isolation": "threads"}
```

Sequential execution:

```json
{"workers": 1, "isolation": "sequential"}
```

Every worker shares the immutable dataset and loaded plugin function table, but
every trial creates and destroys its own strategy handle and report. A
`processes` value is reserved in the schema for future cross-plugin portfolio
execution; the current single-strategy CLI does not launch process workers.
`timeout_seconds` and `fail_fast` are also reserved and currently rejected so
they cannot appear to take effect without real cancellation semantics.

### Logical batches and prefix pruning

```json
{
  "workers": 8,
  "isolation": "threads",
  "batch_size": 4,
  "batch_lag": 1,
  "pruner": "halving",
  "pruner_rungs": [0.25, 0.5],
  "pruner_eta": 3
}
```

| Execution field | Default | Contract |
| --- | --- | --- |
| `batch_size` | `workers` | Positive integer, at most 1,000,000. Proposal size, not thread count. |
| `batch_lag` | `0` | `0` commits b before proposing b+1; `1` proposes b+1 from history through b-1. |
| `pruner` | `"none"` | `none`, `median`, or `halving`. |
| `pruner_rungs` | `[0.25, 0.5]` | Strictly increasing fractions in (0, 1); the full window is implicit. |
| `pruner_eta` | `2` | Integer >= 2; minimum finite history at each rung. Halving retains the best ceil(n/eta); eta=2 uses the median. |

The native equivalents are `--batch-size`, `--batch-lag`, `--pruner`,
`--pruner-rungs 0.25,0.5`, and `--pruner-eta`. Median always uses the median;
its eta only selects history warmup. `--scheduler-stats FILE` writes timing
diagnostics outside the fingerprinted result and has no StudySpec equivalent.

For fixed provenance and explicit batch size, completed `trials[]` reproduce
byte-for-byte with 1, 2, 4, or 8 workers. Without an explicit size, changing
workers intentionally changes the default logical size, as in 0.1.x. Lag-one
TPE requires constant liar enabled. Cancellation/deadline/timeout truncation and
progress-line arrival order are not deterministic replay inputs.

Pruning reruns the exact engine batch path on ceil(fraction * input bars),
deduplicating prefixes that round to the same count. Cuts are frozen from
committed earlier batches at that rung; ties survive. Rung scoring uses the
study objective, and constraints apply only on the full window. A `pruned`
trial keeps partial metrics and objective, is not feasible or a best-trial
candidate, and consumes one trial from the requested budget. It is abandoned
rather than used to train the adaptive sampler.

Pruning adds a `pruning` trial object with `method="prefix_rerun"`,
`rungs_completed` (prefix rungs only), `cut`, `bars_processed_total`,
`script_bars_processed_total`, and `magnifier_sample_ticks_total`. These totals
include all reruns, whereas `backtest` describes only the last executed window.
Use the cumulative totals for compute metering, including on surviving trials.
Pruned trials remain billable trials unless the consuming application explicitly
chooses another policy. Progress lines use exactly the same terminal object.

The native `--trial-timeout-seconds` watchdog spans all prefix reruns in a trial,
not each rung separately. Surviving the default quarter/half/full schedule
requires roughly 1.75 times the full-window bar work; account for that when
choosing a timeout for a pruning-enabled study.

Pruning and pipelining stay opt-in: changing feedback or cutting prefixes can
change the best found result. See [measurements and limitations](batching.md).

### Native-only execution controls (0.2.0)

The native executable accepts additional flags without extending StudySpec v1:

- `--syminfo FILE`: optional positive finite `mincontract` (since 0.4.1), `mintick`
  and `pointvalue`, plus NUL-free string `timezone` and `session`, in flat JSON or
  `{"syminfo": {...}}`. Apply after inputs/overrides, in
  mincontract/mintick/pointvalue/timezone/session order. `mincontract` is the
  instrument lot-size grid (TradingView `syminfo.mincontract`): a number reaches
  the engine as the metadata keys `qty_step` (order quantities are floored to the
  grid) and `mincontract`; `null` or absent means no grid. In the object that is
  read, any other value (zero, negative, string, boolean, array, object, or a number
  outside the double range such as `1e999`) is an initialization error (exit 1,
  `syminfo.mincontract must be a positive finite number`); bare `NaN` and `Infinity`
  are not JSON and fail earlier (exit 1, `invalid JSON at byte N`, which does not name
  the key). A plugin without `strategy_set_syminfo_metadata` never runs gridless: with
  a `mincontract` each trial fails with a `trial_error` that names the key, and the run
  exits 2 because no trial is feasible. Missing fields and empty strings keep engine
  defaults; other catalog keys are ignored. Symbol and chart timezones are distinct.
- `--progress-fd N`: inherited writable descriptor for one flushed terminal-trial
  JSON object per line. One writer prevents interleaving. Each object equals its
  final `trials[]` entry; drain pipes concurrently and do not assume completion
  order matches trial-ID order.
- `--max-wall-seconds S`: positive finite wall seconds, including native
  initialization. Workers check before claiming a candidate and adaptive samplers
  check before a batch. SIGTERM/SIGINT likewise stop new work. Existing in-flight
  trials can finish, and unstarted candidates never become terminal records.
- `--record-metric PATH`: repeatable extra report metric, validated before
  execution and recorded under the exact path spelling in every trial's `metrics`.
  Unavailable values are `null`; extra metrics do not affect scoring or feasibility.
- `--trial-timeout-seconds T`: positive finite per-trial wall seconds from worker
  claim. A watchdog emits one `trial_timeout` record, writes final JSON from
  already-terminal trials plus that record, then `_exit(3)` without worker joins.
  The entire study aborts; other still-running/unstarted trials are excluded.

All terminal trials expose `backtest.magnifier_sample_ticks_total` (zero when
unavailable). The status set is `ok`, `constraint_violation`, `objective_error`,
`constraint_error`, `engine_error`, `trial_error`, and `trial_timeout`. Stop reasons
are `trial_budget_reached`, `search_space_exhausted`, `sampler_stopped`,
`cancelled`, `deadline`, and `trial_timeout`; cooperative stops override the usual
budget/coverage reason without fabricating pending records.

Exit codes are 0 if a feasible best trial exists, 1 for initialization/I/O failure,
2 if no feasible trial exists, and 3 on a trial timeout even if an earlier trial
was feasible. Cooperative stops can write an empty trial table and exit 2.
Final JSON is emitted to stdout and to native `--output FILE`, including stop
paths. `trials_completed` includes the timeout terminal record; coverage and best
selection use only the terminal table. Timeout records cannot be feasible and
make `exhaustive_equivalent` false.

These controls are not forwarded by the Python CLI. The public Python preparation
API `prepare_run(study_path, engine_root, cache_dir)` returns native argv and
artifact JSON without launching, so callers may append the controls and execute
the native process directly. Optional `native`, `compiler`, and `eigen_include`
keywords preserve CLI overrides. `timeout_seconds` remains a rejected reserved
StudySpec field; it is not an alias for either native wall cap.

## Coverage and result provenance

Candidate-policy and coverage fields are part of replay provenance alongside
sampler implementation, sampler configuration, seed, batch size/lag, pruning policy, artifact
key, and search-space definition:

| Field | Contract |
| --- | --- |
| `candidate_policy` | Requested `sampler.candidate_policy`. |
| `candidate_policy_implementation` | Stable implementation identity; compare it when replaying a proposal sequence. |
| `search_space_finite` | Whether the declared space has an exact finite cardinality. |
| `search_space_cardinality` | Exact Cartesian cardinality for a finite space; absent/null when no finite value exists. |
| `trials_requested` | Positive trial budget from StudySpec. |
| `trials_completed` | Number of terminal trial records emitted. |
| `unique_candidates_attempted` | Distinct complete parameter vectors represented by terminal trials. |
| `duplicate_proposals_skipped` | Internal duplicate proposals rejected before trial execution; they do not consume trial budget. |
| `remaining_candidates` | Finite cardinality minus unique attempted vectors; absent/null for a non-finite space. |
| `search_space_exhausted` | Whether no unattempted vector remains in a finite space. |
| `stop_reason` | Machine-readable reason that candidate generation stopped. |
| `full_parameter_coverage` | Every vector in the declared finite domain has a terminal trial record. |
| `exhaustive_equivalent` | Full parameter coverage and every trial status is `ok` or `constraint_violation`. |

Full parameter coverage means that every declared complete vector has a
terminal trial record. It is deliberately weaker than objective coverage. A
constraint violation or failed evaluation remains seen and can coexist with
full parameter coverage, but a failure supplies no comparable objective.

Consequently, `candidate_policy="exhaustive"` does not alone certify equality
with the best result of a successful grid run. `exhaustive_equivalent` remains
false after any pruning, engine, objective, constraint-evaluation, serialization, or
other trial error. When it is true, grid-best equivalence additionally assumes
the same deterministic artifact, dataset, runtime settings, objective, and
constraint policy. Adaptive TPE and grid may visit the same set in different
orders, so intermediate best-so-far results need not match even when their final
best result does.

## Resolution and validation rules

1. All file paths resolve against the directory containing `study.json`.
2. Source strategies are transpiled/compiled once per compatible artifact key.
3. `transpile_full()` supplies input title, type, default, and any literal
   bounds/step/options retained in the artifact manifest.
4. `search_space` explicitly selects the tunable inputs.
5. `fixed_inputs` are never sampled and cannot overlap a tunable input.
6. Strategy overrides remain fixed in v1; optimizing override fields requires a
   future explicit override search space.
7. Candidate types/ranges are validated before C ABI serialization.
8. Objective and constraint expressions compile before any trial starts.
9. Finite candidate policies resolve exact cardinality and validate their trial
   budget before any trial starts.
10. Unknown dataset IDs, duplicate strategy/dataset identities, invalid numeric
   bounds, unknown fields, and missing required files are initialization errors.

## Explicitly later

- portfolio mode and multiple strategies/datasets;
- account-equity aggregation and allocation optimization;
- registered custom-objective lookup from JSON;
- persistence, resume, and worker recovery;
- conditional/hierarchical spaces;
- multi-objective directions and Pareto output;
- walk-forward folds;
- true shared-account execution.
