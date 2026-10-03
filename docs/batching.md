# Deterministic batching and prefix pruning (0.3.0)

## What changes, and what does not

The default remains the 0.1.x proposal schedule: `batch_size=workers`,
`batch_lag=0`, `pruner=none`. Explicit batch size decouples execution resources
from optimizer decisions. A persistent FIFO pool replaces per-batch thread
creation; workers execute, while one coordinator owns proposals, ordered
feedback, and pruning history. No per-trial subprocess is introduced.

The existing shared-work design was confirmed before changing it. At the
0.1.x baseline commit `fd9ba82`:

- `src/cli/main.cpp:829` constructs the objective and constraint expressions
  before trials; `:839` loads one plugin, and `:840` loads one immutable CSV.
- `src/cli/main.cpp:613` implements concurrent indexed batch execution. The
  adaptive loop at `:868` asks a worker-sized set, evaluates at `:876`, and
  tells in trial-ID order at `:885`. Therefore its replay identity included
  worker count, not completion order.
- `src/core/tpe_sampler.cpp:665` puts outstanding parameters only in the bad
  density. `:1335` reserves pending requests; `:1344` and `:1365` implement
  tell/abandon. This was already constant-liar TPE, not a new sampler here.
- `python/pineforge_hpo/cli.py` builds/prepares an artifact once before launching
  the native study. `ArtifactBuilder` caches compilation by artifact identity.
  The native trial loop never transpiles, compiles, or reopens the CSV.

Those boundaries remain. `TrialExecutor::execute_prefix` passes the shared
dataset pointer and a shorter count, without allocating/copying a bar prefix.
Inputs/overrides are serialized once per trial and reused across prefix runs.
A fresh strategy handle is required for each exact prefix rerun; reports are
freed before handles. Objective/constraint/recorded-metric expressions still
compile once per study, and scalar-only report capture avoids unused equity
curve snapshots. The pool only closes the thread-creation gap.

## Replay contract: ordered_batches_v1

Replay provenance includes seed, logical batch size, feedback lag, pruning
policy/rungs/eta, sampler configuration, search space, objective/constraints,
artifact/runtime, and data. Workers are execution capacity, not a replay input
when batch size is explicit. Timing diagnostics are a separate file.

- Lag zero: propose all of batch b, freeze its cuts, finish it, commit/tell its
  trials in ID order, then propose b+1.
- Lag one: initially propose batches 0 and 1 without feedback. After committing
  b, propose b+2. Thus b+1 sees completed history only through b-1 and b's
  parameters as pending. TPE requires constant liar enabled in this mode.
- Pruning cuts follow exactly the same frozen feedback boundary. Neither a
  completed future nor a progress line makes a straggler's batch visible early.

**Proof by induction.** Before a proposal set, the coordinator has identical
ordered committed observations, pending parameters, pruning history, and RNG
state. Ask calls and cut snapshots therefore produce identical candidates and
cuts. A deterministic artifact scores each candidate against the same immutable
prefixes, producing identical terminal records. Ordered commit restores identical
state for the next proposal set. Lag one changes that boundary by a fixed one
batch, never by completion timing. Changing worker count only permutes execution.

The black-box native gates compare raw `trials[]` bytes with 1, 2, 4, and 8
workers for grid, random, TPE, and dlib; they also inject varied per-candidate
delays. Lag tests cover both adaptive samplers and establish that the first
two batches precede feedback. Pruning gates cover both policies and both lags,
partial metrics, full-only constraints, progress parity, and stop behavior.

The real-study benchmark verifies 18 study/mode combinations, each at all four
worker counts (72 replay runs), and repeats every timed mode three times.
All 18 raw-array SHA-256 groups match. Default trial values match the 0.1.x
baseline exactly after removing the additive 0.2.0 magnifier field; explicit
default flags also match omitted flags byte-for-byte.

The guarantee is for completed studies on fixed provenance/toolchain, not
cross-architecture floating point or wall-clock-truncated studies. Deadline,
signal, and watchdog truncation can execute different numbers of trials at
different worker counts. Progress lines deliberately arrive on terminal
completion, so their arrival order is not the fingerprinted trial ordering.

## Pruning and metering

`--pruner median|halving` evaluates quarter, half, then full input windows by
default. Fractions round up and duplicate prefix counts collapse. Full is
implicit. The study objective is evaluated on each prefix; constraints only
apply to the full window. A rung warms up until it has eta finite observations
from eligible earlier batches. Median (and halving eta=2) uses the ordinary
median; halving eta>2 retains the best ceil(n/eta) threshold. Strictly losing
trials prune; ties survive. Median's eta changes warmup, not its quantile.

A `pruned` terminal trial consumes one trial, retains its partial objective and
metrics, is infeasible, and cannot become best. Adaptive samplers abandon it
rather than training on an incomparable prefix score. Prefix observations can
still inform later rung cuts. Exhaustive candidate coverage with pruning is
not exhaustive-equivalent objective coverage.

For every pruning-enabled trial, `pruning.bars_processed_total`,
`pruning.script_bars_processed_total`, and
`pruning.magnifier_sample_ticks_total` sum actual work across all prefix reruns,
including survivors. `backtest` remains the most recently run window, not a
cumulative bill. The application should use cumulative counters for compute
metering and still count a pruned row as a trial unless its owner changes that
policy. The progress line contains the same complete object as the final row.

Stop checks remain before each logical batch/proposal and before each worker
claim. A stopped queued trial does not run or emit a terminal row. The existing
0.2.0 progress writer and timeout watchdog are retained, including exit code 3
and no join after a hung trial. `strategy_request_abort` is available in ABI 4,
but prefix pruning makes decisions between completed exact runs, not halfway
through a report; this release does not replace the runner watchdog contract.

## Measurements (2026-10-03)

Paired baseline `fd9ba82` and 0.3.0 candidate runs used the same artifact, data,
seed 20260718, four workers, and trial budget on an 8-core Ubuntu machine with
GCC 13. Native-process wall time is the median of three runs, including plugin
and CSV loading, excluding the one-time shared artifact compilation. Each
real strategy uses 53,929 ETHUSDT 15-minute bars from the public engine benchmark
assets, 64 TPE trials, and objective net profit minus 0.25 times equity drawdown,
with at least one trade required. The 34-bar threshold example uses its existing
9-trial grid and 0.5 drawdown coefficient. Rungs are 0.25 and 0.5.

`default` means k=4/lag=0/no pruning; `k8` changes only k; `lag1` uses k=4;
`median` uses eta=2; `halving3` uses eta=3; `lag1+halving3` combines both.
Input-bar totals include prefix recomputation. Rank is the baseline best's
virtual rank among this mode's full-window feasible scores, including untimed
shadow evaluations of pruned candidates; `absent` means that parameter vector
was never proposed. Shadow work is excluded from measured wall time/bars.

| Study | Mode | Wall s | Input bars | Best objective | Baseline-best rank/status | Pruned |
| --- | --- | ---: | ---: | ---: | --- | ---: |
| Example | 0.1.x | 0.002411 | 306 | -22.5 | 1 / ok | 0 |
| Example | default | 0.002818 | 306 | -22.5 | 1 / ok | 0 |
| Example | k8 | 0.002728 | 306 | -22.5 | 1 / ok | 0 |
| Example | lag1 | 0.002448 | 306 | -22.5 | 1 / ok | 0 |
| Example | median | 0.003046 | 353 | -22.5 | 1 / ok | 4 |
| Example | halving3 | 0.003037 | 353 | -22.5 | 1 / ok | 4 |
| Example | lag1+halving3 | 0.003006 | 506 | -22.5 | 1 / ok | 1 |
| BB/RSI | 0.1.x | 0.897340 | 3,451,456 | 1.3750000000020464 | 1 / ok | 0 |
| BB/RSI | default | 0.906564 | 3,451,456 | 1.3750000000020464 | 1 / ok | 0 |
| BB/RSI | k8 | 0.880424 | 3,451,456 | 1.3750000000020464 | 1 / ok | 0 |
| BB/RSI | lag1 | 0.832207 | 3,451,456 | 1.3750000000020464 | 1 / ok | 0 |
| BB/RSI | median | 0.827673 | 1,968,467 | 1.3750000000020464 | 1 / ok | 54 |
| BB/RSI | halving3 | 0.800947 | 1,860,608 | 1.3750000000020464 | 1 / ok | 55 |
| BB/RSI | lag1+halving3 | 0.785375 | 2,561,689 | 1.3750000000020464 | 1 / ok | 46 |
| Volatility | 0.1.x | 1.481162 | 3,451,456 | 6530.4349999999795 | 1 / ok | 0 |
| Volatility | default | 1.471613 | 3,451,456 | 6530.4349999999795 | 1 / ok | 0 |
| Volatility | k8 | 1.558968 | 3,451,456 | 5922.33750000004 | 1 / absent | 0 |
| Volatility | lag1 | 1.460384 | 3,451,456 | 5922.33750000004 | 1 / absent | 0 |
| Volatility | median | 2.317233 | 4,098,675 | 5922.33750000004 | 1 / absent | 27 |
| Volatility | halving3 | 1.937087 | 2,750,440 | 6551.819999999991 | 2 / absent | 42 |
| Volatility | lag1+halving3 | 1.397184 | 3,127,946 | 5937.942500000003 | 1 / absent | 38 |

The public real-strategy sources are
`068-ta-bb-rsi-mean-reversion-01` and
`085-ta-stdev-sma-expansion-break-01` in the pinned engine's benchmark assets.
These are a bounded paired-seed measurement, not a claim of universal optimum
preservation or speedup. Millisecond example times are mostly startup noise.

### Why lag one ships, but stays opt-in

Scheduler idle capacity (worker-seconds minus measured task busy-seconds)
includes barrier stragglers, proposal/feedback, and startup, not exclusively
engine time. It drops from 15.67% to 2.75% on BB/RSI and 16.84% to 2.17% on
volatility with lag one. BB/RSI wall time improves; volatility's improvement
is small and its best objective is worse. Fixed lag is useful explicit control,
not a silent replacement for the compatible feedback schedule.

### Why prefix reruns (A), not streaming (B)

The benchmark probes the first candidate and baseline-best candidate with
the engine's streaming begin/push/fill API at the same three prefixes. It
compares every ABI trade field and exact objective, not just trade counts.
Streaming has no bar magnifier and close-only semantics, so it cannot replace
general batch execution without study-specific equivalence evidence.

| Study/candidate | A seconds | B seconds | A input bars | B input bars | Trade parity | Objective parity |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| Example / first | 0.000518 | 0.000226 | 60 | 34 | all rungs | all rungs |
| Example / best | 0.000535 | 0.000225 | 60 | 34 | all rungs | all rungs |
| BB/RSI / first | 0.060736 | 0.131253 | 94,377 | 53,929 | all rungs | all rungs |
| BB/RSI / best | 0.057433 | 0.096656 | 94,377 | 53,929 | all rungs | quarter differs |
| Volatility / first | 0.084669 | 2.228788 | 94,377 | 53,929 | all rungs | all rungs |
| Volatility / best | 0.128185 | 2.841356 | 94,377 | 53,929 | all rungs | all rungs |

Trade parity alone is insufficient: the BB/RSI quarter-window objective is
42.51999999999998 in A versus 42.51749999999765 in B.
Streaming is also slower on both real strategies despite fewer bars. Therefore
this release always uses A. No unverified B fast path ships, so no production
startup sampling/fallback heuristic is needed. A remains valid with magnifiers
and runtime settings and shares bars without recomputing CSV parsing.

### Defaults and quality

Pruning saves up to 46.09% of input bars on BB/RSI and preserves its exact best
answer. However, median pruning increases volatility compute, and feedback
changes can miss the baseline best there. Even the tiny example processes
more bars with prefix reruns. All shadow checks find no pruned proposed
candidate exceeding that mode's selected best, but that does not prove the
adaptive search would have proposed the same optimum without pruning.
**Pruning remains off by default**, as does pipelining; there is no general
same-answer guarantee for these opt-in modes.

## Reproducing the evidence

See `benchmarks/batching/run_benchmark.py` and
`benchmarks/batching/compare_rungs.cpp`. Generated JSON, CSV, metadata, hashes,
and artifacts stay under ignored `build/`. The metadata records artifact/input
hashes, executable hashes, replay groups, and A/B probes. The benchmark rejects
default compatibility failures, repeat nondeterminism, and worker mismatches.
Run the commands in the [benchmark guide](../benchmarks/README.md).
