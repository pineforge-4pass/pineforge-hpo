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

`sampler.config.history_switch` is absent or `null` by default, meaning never switch:
TPE uses exact 0.3.0 full history throughout the study. A positive uint64 maps to
native `--tpe-history-switch N` and explicitly opts into bounded TPE. Below that
completed usable observation count, TPE uses the exact 0.3.0 full-history estimator.
At the count, it switches to bounded density tables with 25 elites, 64 recent
non-elites, and 448 seeded older non-elites by default. This count is independent of
worker count, issued/abandoned candidates, and wall time. N=1,000 is a reasonable
opt-in for 10k–1M studies, trading refinement quality for history-flat acquisition.
Measured median-regret-ratio geomeans are 1.036492 at 3k versus 0.3.0 and 1.031570
at 10k versus the full-history benchmark variant. A larger override has higher
memory and acquisition cost before the switch: legacy 16D snapshot asks measured
27,652 microseconds at 1,024 observations and 112,201 at 4,096.
Results and TPE terminal records persist
the configured value as `tpe_history_switch`; it is not a trial or wall-time cap.

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
| `history_switch` | `null` | Never switch when absent/null; positive uint64 opts into bounded models via native `--tpe-history-switch N`. |
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

- `--syminfo FILE`: optional positive finite `mincontract` (since 0.3.1), `mintick`
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

## Continuation and space identity (0.5.0)

Continuation is a CLI/job input, not a new StudySpec field. Pass `--warm-start FILE`
to `run` or `space-info --spec STUDY`. The new study must have the same parameter
names, kinds, numeric bounds/steps, categorical choice order/types, log flags,
objective expression, direction and constraints. Changing the trial budget, seed,
workers, batch size, HPO version or engine/codegen pins does not change its space hash.
This identity does **not** certify unchanged market data, fixed inputs or strategy
behavior: the caller is responsible for keeping observations semantically comparable.

The input is auto-detected as binary v2 blocks (0.6.0), terminal-trial JSONL,
a standalone trial array, or a complete result object. Result objects combine
`warm_start_trials` with new `trials`.
Space metadata is intentionally repeated on each new trial so a standalone trial
array remains self-describing. Budget for that per-line storage/transport overhead;
the 256 MiB input cap is a byte cap, not a constant-memory import guarantee.
Trial IDs must be unique uint64 integers leaving room for a continuation ID, parameters
must lie in the declared space, objectives/rung scores must be finite or null, and
statuses must be `ok`, `constraint_violation`, `engine_error`, `objective_error`,
`constraint_error`, `trial_error`, `trial_timeout`, `pruned` or `partial`.
Feasibility must agree with status. Unknown statuses fail closed at initialization.

Legacy complete results can supply an embedded `study_spec` or a `study` JSON path
(relative to the parent file) when declarative `space` was not recorded. Legacy bare
JSONL without recorded space cannot prove bounds/objective compatibility and is
refused; use the complete parent result or attach its normalized recorded space to
each trial. Older hash versions are accepted by recomputing from that recorded space,
not by requiring a digest from the current package version.

### Canonical hash v1

`space_hash` is lowercase SHA-256 of UTF-8 JSON, without trailing newline, with sorted
object keys, no insignificant whitespace, unescaped non-ASCII characters and standard
JSON string escaping. The root contains exactly `space_hash_version: 1`, `parameters`
and `objective`:

- `parameters` maps sorted names to `kind`: `integer`, `real`, `boolean` or
  `categorical`.
- Integer entries also contain `low`, `high`, `step` as decimal **strings**, and a
  Boolean `log`; omitted step/log normalize to `1`/`false`.
- Real entries contain `low`, `high` and optional `step` as **16 lowercase hex digits
  of IEEE-754 binary64 bits** (`step: null` for continuous), and Boolean `log`.
  Omitted log normalizes to false; signed zero normalizes to positive zero.
- Boolean entries contain only `kind`.
- Categorical entries contain ordered `choices`: each is `[type, value]` with type
  `integer`, `real`, `boolean` or `string`. Integer/real values use the same decimal
  string/binary64-hex encodings; Boolean/string values remain JSON Boolean/string.
- `objective` contains exact `expression`, `direction` and lexicographically sorted
  expression-string `constraints`. Constraint order does not affect identity; all
  other order-sensitive values, including choices, are preserved.

The recorded declarative `space` retains normal numeric JSON values, not hash-encoded
strings, so future versions can recompute a different canonical form. Changing the
definition requires bumping `space_hash_version`; package/pin bumps do not.
The golden digest for `examples/single_strategy/study.json` is
`1188c07588652f2b365a1fc233815d5c51d350e92106051309d9e84ad586071a`.
Python and native golden/parity tests pin this definition.

### Sampler and replay rules

Grid scans its original Cartesian ordinal order and skips every parent vector, even
failed/pruned ones; sparse histories therefore resume at the first untried ordinal.
`without_replacement` reserves every attempted vector across parent plus new trials.
If the combined budget reaches cardinality, these finite policies produce the same
final set as a single exhaustive run. Neither rule depends on parent trial IDs being
contiguous. IDs always resume after the maximum ID.
Budgets that exceed the remaining uint64 trial-ID capacity fail preflight with exit 4.
Native wall-only continuations stop at that capacity rather than wrapping IDs, and
refuse before loading plugins/data when no additional trial ID is available.
An `exhaustive` continuation's additional budget must equal the remaining cardinality;
`without_replacement` cannot request more than remains. Incompatible warm budgets
are refused with exit 4 before compilation or billing.

Random uses `continuation_seed(seed, warm_trials)`: for nonzero warm count, unsigned
64-bit SplitMix-style mixing of `seed XOR (warm_trials + 0x9e3779b97f4a7c15)`, using
the `0xbf58476d1ce4e5b9` and `0x94d049bb133111eb` multipliers and shifts 30/27/31.
Zero warm trials preserves the original seed. Parent vectors are rejected; after
64 unsuccessful finite draws a deterministic untried-ordinal fallback ensures
progress. New random points can repeat one another under `sampler_default`.
The finite ordinal fallback can stop a random continuation early once it has visited
every non-parent ordinal; its combined coverage is then complete.

TPE trains only on finite completed feasible objectives, as in live `tell()`;
infeasible/failed/pruned/partial trials follow live `abandon()` semantics. Imported
observations count toward startup and the history-switch threshold. Available
`pruning.rung_scores` restore the bounded prefix-pruner histories, not final-objective
observations. Changing pruning rungs invalidates their meaning; keep the parent's
prefix schedule for pruning equivalence.

Since 0.6.1, TPE imports the ordered parent without proposing any historical trial.
A complete result's `tpe_sampler_state`, or a warm-v2 `PFHSTATE` extension, restores
the exact suggestion RNG, reservoir RNG, finite fallback cursor, retained observations
and bounded cache membership when configuration, seed and complete typed history match.
The checkpoint is checksummed and contains a typed-history fingerprint. Full-history
models rebuild from the same chronological rows; bounded caches rebuild from their
recorded fitting IDs. Matching future lag-zero ask/tell schedules reproduce the
uninterrupted stream bit-for-bit, including a partial final parent batch. The checkpoint
requires zero outstanding candidates and the complete parent history, not just best-K rows.

Row-only v0.5/v0.6 parents, mismatching checkpoints and lag-one continuations instead
reconstruct trainable history in ID order with the derived continuation RNG and the
same history switch and reservations. This intentionally replaces the former complete
matching-batch replay contract for legacy TPE parents. Numeric rejection sampling and
finite retries consume a model-dependent RNG stream; winning terminal rows do not
record its cursor. Advancing a fixed number of RNG draws per trial cannot be exact.
The weaker invariant is deterministic, bounded history reconstruction with
correct startup progress and exclusion of all reserved vectors. Exact byte replay
holds for the same parent bytes, seed, sampler configuration, batch size and worker
count (excluding explicitly requested timing sidecars).

The app may choose TPE `batch = clamp(floor((trials - exploration) / 20), 2, workers)`
for worker counts 2..8, and random/grid `batch = workers`. Batch size is not part of
space identity; matching it matters for TPE proposal equivalence, not compatibility.

## Binary warm format v2 (0.6.0)

This is a sampler observation format, not a backtest report or resumable execution
checkpoint. Store **every earlier attempted trial**, including unsuccessful and
duplicate-parameter trials. Each chunk is one independently self-describing block.
A file is one or more complete blocks concatenated byte-for-byte, with **no** outer
header, separators, alignment padding, compression or footer. Blocks can arrive in
any order; IDs determine observation order. Every block must match the current study.

### Optional sampler-state block (0.6.1)

An independent extension block starts with the eight ASCII bytes `PFHSTATE`, followed
by a little-endian uint64 payload length and that many UTF-8 checkpoint bytes. There
is no padding. The payload is `PFHTPE1\n`, a 64-character lowercase SHA-256 digest,
`\n`, and the versioned state. The digest covers the state after the second newline.
The payload must be nonempty and at most 16 MiB; at least one ordinary trial block is
required. No-state warm-v2 golden bytes are unchanged. v0.6 and older binary readers
reject this extension; omit `sampler_state` when producing a row-only export for them.

State blocks can appear anywhere among row blocks. The checkpoint with the greatest
attempted-row count wins, independently of block order; identical duplicates are
allowed, conflicting checkpoints for the same attempted-row count are rejected. The sampler still
verifies the selected checkpoint against the complete imported history and current
configuration. A stale or incompatible checkpoint causes history reconstruction,
never proposal replay. Native `warm-encode` preserves the state from complete result
JSON. Python writers accept it through the keyword-only `sampler_state` argument.

### Block header

All multibyte integers and IEEE-754 binary64 values are **little-endian**. Offsets
are relative to the start of each block. The fixed header is exactly 80 bytes.

| Offset | Bytes | Type/value |
| --- | --- | --- |
| 0 | 8 | Magic: hex `50 46 48 57 41 52 4d 00` (`PFHWARM\0`) |
| 8 | 2 | uint16 format version: `2` |
| 10 | 2 | uint16 flags: `0` |
| 12 | 4 | uint32 header bytes: `80 + 4 * P` |
| 16 | 8 | uint64 total block bytes, including header/descriptors/payload |
| 24 | 8 | uint64 trial count `N`, strictly positive |
| 32 | 4 | uint32 parameter column count `P`, equal to study input count |
| 36 | 4 | uint32 objective column count `O`: exactly `1` in 0.6.0 |
| 40 | 4 | uint32 constraint column count `C`, equal to study constraint count |
| 44 | 4 | uint32 reserved: `0` |
| 48 | 32 | Raw SHA-256 bytes of canonical space hash v1, not ASCII hex |
| 80 | `4 * P` | Parameter descriptors, in lexicographic UTF-8 parameter-name order |

Each descriptor is `uint8 kind, uint8 encoding, uint16 reserved=0`:

| Kind | Domain | Encoding |
| --- | --- | --- |
| 1 | Integer (including log integer) | 1: int32 grid index |
| 2 | Real with a step | 1: int32 grid index |
| 2 | Real without a step (including log real and fixed bounds) | 2: float64 original value |
| 3 | Boolean | 1: int32 choice index, false=0 and true=1 |
| 4 | Categorical | 1: int32 zero-based index in declared typed choice order |

Integer grid index `i` decodes to `low + i * step`. Stepped reals decode with
binary64 fused multiply-add `fma(double(i), step, low)`; a last-grid value above
the upper bound by permitted endpoint rounding is clamped to `high`, exactly as
the native search space does. Indices must be nonnegative and less than the
dimension cardinality; at most 2^31 values are supported per indexed dimension.
Log parameters store original units/indices, never logarithmic model coordinates.
The converter refuses noncanonical stepped-real values rather than losing bits.
Continuous values must be finite and in range; signed zero is preserved and is
distinct in the exact tried-vector index, as in the legacy canonical key.

### Payload columns

Starting at `header_bytes`, write these complete columns consecutively:

1. `N` **uint64 trial IDs**. IDs are intentionally widened from the requested
   int32: the existing API permits IDs above 2^31 and 2^53. Values must be unique
   across all blocks and less than `UINT64_MAX`, leaving a continuation ID.
2. `N` uint8 trial states.
3. Each of the `P` parameter columns, using its descriptor, in descriptor order.
4. `O=1` objective column: `N` float64 values in the original objective direction.
5. Each of the `C` constraint columns: `N` float64 expression values, in stable
   lexicographic UTF-8 constraint-expression order (duplicates retain their study
   order). This matches the order-insensitive constraint identity in hash v1.

No column has a name string, JSON, metrics, error text, timestamp, pruning rung or
trial envelope. Names and categorical values are resolved from the matched study.
Each null objective/constraint is the **only accepted nonfinite pattern**:
canonical quiet NaN bits `0x7ff8000000000000` (bytes `00 00 00 00 00 00 f8 7f`).
Finite values retain their exact bits, including negative zero. Infinities and other
NaN patterns are refused. Missing legacy constraint arrays become null, not inferred
numeric evaluations. New `constraint_values` arrays are stored in study order in
JSON, then permuted into canonical binary order. State determines feasibility;
numeric constraint columns do not retroactively change v0.5 TPE behavior.

| State | Legacy status | Feasible | Trains TPE |
| --- | --- | --- | --- |
| 0 | `ok` | yes | only if the objective is finite |
| 1 | `constraint_violation` | no | no |
| 2 | `engine_error` | no | no |
| 3 | `objective_error` | no | no |
| 4 | `constraint_error` | no | no |
| 5 | `trial_error` | no | no |
| 6 | `trial_timeout` | no | no |
| 7 | `pruned` | no | no |
| 8 | `partial` | no | no |

`block_bytes = header_bytes + N * (8 + 1 + sum(parameter_widths) + 8*O + 8*C)`.
Reject unsupported versions/flags/reserved fields, inconsistent counts/descriptors,
overflowing lengths, partial/trailing bytes, duplicate IDs, invalid states/scalars
and any block hash mismatch with exit **4**, `warm-start incompatible: ...`, before
compilation/plugin/data loading or billable output. Finite exhaustion remains exit
**5**. `space-info` succeeds and reports zero remaining for exhausted histories.

### Loading, integrity and ingest

The native loader uses a read-only mmap and validates fixed-width columns directly;
it does not parse or retain JSON. TPE holds shared row references, not one candidate
map per trial. Preserve the mapped file unchanged until the run exits; upload/finalize
objects before mapping, never append/truncate/rewrite an active input. Total rows are
limited to `UINT32_MAX`; IDs are still uint64. Sorted uint32 row references provide
collision-free exact tried-vector lookup, retaining all duplicate observations while
counting each vector once. This costs at most 4 bytes/row, plus another 4 bytes/row
only when ID sorting is needed. A Cartesian bitmap is unsuitable for continuous or
huge spaces and cannot represent all those vectors; no separate tried bitmap is stored.

The app keeps SHA-256 per immutable object and verifies objects before concatenation.
There is no redundant per-block checksum. The result's `source_sha256` hashes the
exact concatenated input bytes; native `--warm-digest` also requests this digest.
`space-info` (including `--warm-details` preflight) performs load/validation/tried-index
construction without payload
hashing. At 1M rows, five float64 inputs need 57,000,100 bytes plus a 4-MB tried index;
32 inputs with four indexed and 28 float64 columns need 257,000,208 bytes plus that
index. All-float64 32-input blocks need 273,000,208 bytes. These are format sizes,
not promises about OS RSS, TPE density work, replay, result rendering or provenance hashing.

```bash
pineforge-hpo warm-encode --spec study.json --input parent.jsonl \
  --output parent.warm --block-trials 100000
cat chunk-1.warm chunk-2.warm > history.warm
pineforge-hpo space-info --spec study.json --warm-start history.warm
pineforge-hpo run study.json --warm-start history.warm
```

```python
from pineforge_hpo.study_spec import load_study_spec
from pineforge_hpo.warm_binary import write_warm_block

study = load_study_spec("study.json", continuation=True)
with open("chunk.warm", "wb") as output:
    write_warm_block(output, study, ingested_trials)
```

Golden vectors are `tests/fixtures/warm_v2_spec.json`, `warm_v2_trials.json` and
`warm_v2_golden.json`: one 247-byte block and the same rows in two blocks, exact
hex and SHA-256. They exercise every encoding, non-int32/non-exact-double IDs,
nulls and signed zero. Native and Python writers must match these byte-for-byte.
The release additionally compares actual v0.5 JSON suggestions with v0.6 JSON,
single-block and reordered multi-block inputs, using packed binary64/uint64
suggestion hashes. See `benchmarks/warm/README.md` for reproduction and boundaries.

JSON/JSONL v0.5 inputs stay accepted for this release with the existing 256-MiB cap.
Binary v2 currently supports grid/random/TPE with no active prefix pruner; use JSON
when restoring pruning rungs. `dlib_global` warm start remains unsupported. Warm
rows never go to fd 3. A final `trials_out=all` result reconstructs minimal ancestor
records only at rendering time; summary/none users should retain the original
binary plus independently encoded new-trial chunks for subsequent continuation.

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
