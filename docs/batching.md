# Deterministic batching and prefix pruning (0.3.0)

## Scaling in 0.4.0

Logical batch/lag semantics below remain unchanged. Bounded TPE model state and
cached acquisition reduce coordinator work; opt-in lag one overlaps proposal
work with the previous executing batch. The runner now uses every worker for
deadline-only adaptive studies, generates nonadaptive candidates lazily, and
keeps only best-k terminal payloads when requested. Enabled pruners bound each
rung's reference history to its latest 1,024 finite scores; disabled pruning
keeps no rung history. The progress writer uses bounded backpressure and emits
complete terminal records in increasing ID order, independent of worker timing.
See [the before/after profile and quality evidence](../benchmarks/scaling/README.md)
and [the native output contract](api.md).

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

## Choosing batch size, lag and pruning

For expensive objectives, start with `--batch-size` equal to the worker count
(8 in the study below), `--batch-lag 0`, and `--pruner none`. Choose batch size 1
when search quality comes first or objective evaluations are cheap. Larger
batches, lag and pruning are opt-in trade-offs, not free speed.

A separate pure-math benchmark compared 36 hard problems over 30 paired seeds
at 100 and 300 trials with 8 workers. With pruning disabled, the table shows
relative residual-regret increases against sequential TPE (batch 1); higher
regret means worse search quality.

| Mode | Regret increase at 100 trials | Regret increase at 300 trials |
| --- | ---: | ---: |
| Batch 8, lag 0 | +5.4% | +0.8% |
| Batch 16, lag 0 | +16.1% | +4.3% |
| Batch 32, lag 0 | +27.7% | +23.0% |
| Batch 8, lag 1 | +20.1% | +9.2% |

Batch 8, lag 0 used about 6.9 times fewer 8-worker evaluation rounds at matched
quality, targeting sequential TPE's median final regret at 100 trials.
Separately, a partial-sum Rastrigin20 check with batch 8, lag 0 at 300 trials
compared each pruner with no pruning: median used 12.7% less work but increased
regret by 21.4%; halving used 40.7% less work but increased regret by 28.1%.
Those pruning results use synthetic partial-sum fidelities, not backtest
prefixes; validate the learning curve for your own objective before enabling
pruning.

Rounds are an equal-duration evaluation-wave proxy, not measured backtest wall time.
The confidence intervals do not prove equivalence or rule out meaningful quality losses.

## Replay contract: ordered_batches_v1

Every sampler emits `replay_contract: "ordered_batches_v1"` for fresh-run
proposal/feedback ordering. This is not a row-only TPE continuation guarantee.
Since 0.7.0, TPE separately emits `continuation_contract: "sampler_checkpoint_v2"`: a
matching complete checkpoint/numerical build and matching future lag-zero schedule
preserve the exact sequence. Row-only parents (including JSONL) rebuild history and
are never exact by virtue of matching batch boundaries alone. Grid/random emit
`continuation_contract: "ordered_batches_v1"` and keep their prior continuation semantics.

TPE `max_threads` controls only fit/score resources. Its default is min(8, CPUs
allowed by affinity/cgroup quota); a persistent pool reuses workers across EI draws,
and creation failures fall back to serial. 1 and 8 workers produce identical bits.
The limit is not a sampler-state/replay identity input.

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

## Sobol replay

The `sobol` sampler has no feedback: candidate `n` is a pure function of the run identity and `n`,
so worker count, batch size and lag change only scheduling, never a value. Repeated runs and any
worker count give identical result bytes at a fixed batch size and lag with no external stop. With
`--no-improvement-trials` the terminal frontier follows the ordered drain exactly as for the other
samplers, so the point-by-ID claim (every retained row equals the generator at its ID) is
unconditional while the number of rows depends on the fixed batch size and lag. Deadline, signal
and watchdog stops truncate at schedule-dependent points. See [Sobol](study-spec.md).

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

Cancellation/deadline checks remain before each logical batch/proposal and before each worker
claim. A queued trial skipped by those stops does not run or emit a terminal row. The existing
0.2.0 progress writer and timeout watchdog are retained, including exit code 3
and no join after a hung trial. `--trial-timeout-seconds` spans the entire trial,
including every prefix rerun; it does not restart at each rung. A surviving
trial with the default rungs processes roughly 1.75 times the full-window bar
work, so raise the trial timeout when enabling pruning if needed.
`strategy_request_abort` is available in ABI 4,
but prefix pruning makes decisions between completed exact runs, not halfway
through a report; this release does not replace the runner watchdog contract.

## No-improvement stopping

Pass `--no-improvement-trials N` to native `run` or the Python `run` wrapper.
N is an unsigned 64-bit integer; absent or zero disables patience. Negative,
fractional and overflowing values are argument errors. The first feasible finite
objective in the new part establishes a reference best with count zero. Each
subsequent ordered terminal trial consumes one count unless its feasible finite
objective is strictly better in the study's maximize/minimize direction. Ties,
infeasible results, errors and pruned rows never reset the count. Before the first
feasible new-part result, nothing counts toward patience.

No-improvement is judged against the best found in THIS part, not the lineage's best ever.
Warm continuation resets both count and reference best. Parents initialize the
sampler as before; patience is not serialized into the sampler checkpoint. Exact
TPE continuation still requires a matching complete checkpoint/numerical build and
a matching future lag-zero schedule. Random continuation reseeds and excludes
parent candidates; future lag-one TPE rebuilds history. Patience does not strengthen
those existing continuation contracts.

The coordinator checks patience in ordered completion/feedback, independently of
worker arrival and progress-write timing. On the Nth non-improving terminal it
latches that global trial ID and freezes the current and queued batches, whose
highest already-proposed ID is F. It submits no further batch and drains those
batches, including every sampler reservation and terminal record through F.
Patience never cancels worker claims.
Later improvements remain in the report and can become best, but cannot unlatch
the trigger. For fixed batch B and lag L, the tail is bounded by `(L+1)*B-1`, with
smaller tails possible at budget/exhaustion boundaries.

The implicit batch size remains the number of workers.
To get the same stop trial with a different number of workers, pass an explicit batch size.
All other replay inputs listed above must also match. Fatal watchdog/output failure
has priority, followed by observed cancellation/deadline, latched `no_improvement`,
and natural budget/exhaustion. In particular, a trigger on the last budgeted trial
still reports `no_improvement`. The real watchdog retains `trial_timeout` and exit 3;
its timed-out trial is not recovered. Cancellation/deadline can interrupt the drain
using their existing worker-claim behavior.
When a progress pipe is full, the writer can observe cancellation or a deadline during
the final drain with patience enabled or disabled, and the result reports that observed stop.

Enabled final study results add this object:

```json
"early_stop": {
  "patience_trials": 200,
  "trigger_trial_id": null,
  "drained_through_trial_id": 127,
  "reference_scope": "part"
}
```

The trigger is null until latched. `drained_through_trial_id` is the last terminal
ID consumed by ordered feedback, or null before any such result; after an ordinary
patience drain it equals F. A watchdog or other external stop may report only a
partial drain. This snapshot is synchronized with the watchdog's final writer.
Fatal watchdog metadata may name trigger or drain IDs absent from the fatal result's trial list.
Off studies omit the object and retain prior output/checkpoint formats and Python
native argv. A normal patience stop has a feasible new-part result and exits 0;
a study with no feasible result remains unarmed and retains exit 2 at natural stop.

## Measurements (2026-10-03)

Paired baseline `fd9ba82` and 0.3.0 candidate `1b2922a` used the same artifact, data,
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
| Example | 0.1.x | 0.002599 | 306 | -22.5 | 1 / ok | 0 |
| Example | default | 0.002898 | 306 | -22.5 | 1 / ok | 0 |
| Example | k8 | 0.002819 | 306 | -22.5 | 1 / ok | 0 |
| Example | lag1 | 0.002606 | 306 | -22.5 | 1 / ok | 0 |
| Example | median | 0.003363 | 353 | -22.5 | 1 / ok | 4 |
| Example | halving3 | 0.003270 | 353 | -22.5 | 1 / ok | 4 |
| Example | lag1+halving3 | 0.003235 | 506 | -22.5 | 1 / ok | 1 |
| BB/RSI | 0.1.x | 1.406505 | 3,451,456 | 1.3750000000020464 | 1 / ok | 0 |
| BB/RSI | default | 0.979822 | 3,451,456 | 1.3750000000020464 | 1 / ok | 0 |
| BB/RSI | k8 | 0.919068 | 3,451,456 | 1.3750000000020464 | 1 / ok | 0 |
| BB/RSI | lag1 | 0.844415 | 3,451,456 | 1.3750000000020464 | 1 / ok | 0 |
| BB/RSI | median | 0.877268 | 1,968,467 | 1.3750000000020464 | 1 / ok | 54 |
| BB/RSI | halving3 | 0.839100 | 1,860,608 | 1.3750000000020464 | 1 / ok | 55 |
| BB/RSI | lag1+halving3 | 0.796408 | 2,561,689 | 1.3750000000020464 | 1 / ok | 46 |
| Volatility | 0.1.x | 1.524019 | 3,451,456 | 6530.4349999999795 | 1 / ok | 0 |
| Volatility | default | 1.520600 | 3,451,456 | 6530.4349999999795 | 1 / ok | 0 |
| Volatility | k8 | 1.598407 | 3,451,456 | 5922.33750000004 | 1 / absent | 0 |
| Volatility | lag1 | 1.506824 | 3,451,456 | 5922.33750000004 | 1 / absent | 0 |
| Volatility | median | 2.366980 | 4,098,675 | 5922.33750000004 | 1 / absent | 27 |
| Volatility | halving3 | 2.008658 | 2,750,440 | 6551.819999999991 | 2 / absent | 42 |
| Volatility | lag1+halving3 | 1.433000 | 3,127,946 | 5937.942500000003 | 1 / absent | 38 |

The public real-strategy sources are
`068-ta-bb-rsi-mean-reversion-01` and
`085-ta-stdev-sma-expansion-break-01` in the pinned engine's benchmark assets.
These are a bounded paired-seed measurement, not a claim of universal optimum
preservation or speedup. Host scheduling changes absolute wall times between
measurement passes; the bar totals and objective/replay checks are exact.
Millisecond example times are mostly startup noise.

In particular, the BB/RSI baseline-to-default wall gap (1.407 s versus 0.980 s)
is treated as scheduling noise, not an explained speedup. That default path
only replaces per-batch thread creation; the volatility default shows virtually
no wall-time gain. Do not attribute the BB/RSI baseline/default difference to
the persistent pool or use it as a general performance claim.

### Why lag one ships, but stays opt-in

Scheduler idle capacity (worker-seconds minus measured task busy-seconds)
includes barrier stragglers, proposal/feedback, and startup, not exclusively
engine time. It drops from 16.65% to 1.91% on BB/RSI and 17.02% to 2.17% on
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
| Example / first | 0.000566 | 0.000231 | 60 | 34 | all rungs | all rungs |
| Example / best | 0.000539 | 0.000229 | 60 | 34 | all rungs | all rungs |
| BB/RSI / first | 0.062337 | 0.133054 | 94,377 | 53,929 | all rungs | all rungs |
| BB/RSI / best | 0.058977 | 0.098205 | 94,377 | 53,929 | all rungs | quarter differs |
| Volatility / first | 0.085878 | 2.229453 | 94,377 | 53,929 | all rungs | all rungs |
| Volatility / best | 0.132044 | 2.852632 | 94,377 | 53,929 | all rungs | all rungs |

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
