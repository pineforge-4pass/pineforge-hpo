# Real-strategy adaptive HPO validation

This historical validation exercised the complete direct-PineScript path without
copying a third-party strategy or corpus market data into the then-Apache-2.0
repository. Releases up to and including v0.10.0 remain available under Apache-2.0;
starting with v0.11.0, original HPO code uses the
[PineForge Source License 1.2](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/LICENSE).

> **Historical snapshot.** This run was recorded during `0.1.0` development
> with sampler identity `pineforge_product_tpe_v1`, before the finite-candidate
> policy work introduced the current `v2` identities. The source revision and
> raw result JSON were not retained, so its hash is not current-release
> acceptance evidence. The strategy/data hashes and methodology remain useful
> historical validation; release claims must rerun the current executable and
> publish its result provenance.

## Inputs

- Date: 2026-07-18
- Strategy: a third-party long/short Supertrend strategy from a private test set
  (not redistributed)
- Strategy SHA-256: `1d703474287b79be82c18f90451129c912536287b32d7a9051fd53ea51a83072`
- OHLCV: `pineforge-engine/corpus/data/ohlcv_ETH-USDT-USDT_1m.csv`
- OHLCV SHA-256: `db8c1332da093008cfbd063e05db0b33fe8f7fd35d78cf058a366519eb9f6cc5`
- OHLCV range: 2020-01-01 00:00 UTC through 2026-05-04 15:14 UTC
- OHLCV rows: 3,334,340 one-minute bars
- Artifact key: `46cd14b22d180fb5ab8c9faf5c0ad91c272df980c935019d668571f82ee6f2fd`

The Pine source was passed through `pineforge_codegen.transpile_full()`, compiled
once, ABI/symbol checked, and reused for all trials. Tunable inputs were `ATR
Length` as integer `[3, 50]` and continuous `Factor` `[0.5, 8.0]`. `Enable
Strategy=true` and `Lot Size=1.0` were fixed runtime inputs.

The study supplied `input_tf=1` and `script_tf=15`; it did not use the derived
15-minute CSV. Every successful result reported:

```text
input_bars_processed  = 3,334,340
script_bars_processed =   222,295
input_tf_seconds      = 60
script_tf_seconds     = 900
script_tf_ratio       = 15
needs_aggregation     = true
```

At the selected candidate, a one-trial control against the pre-derived 15-minute
corpus file produced bit-for-bit equal objective and selected metric JSON. This
confirms that the real HPO run used PineForge engine timeframe aggregation.

## Study

All three samplers used seed `20260718`, 4 workers, 128 trials, and:

```text
maximize metrics.all.net_profit
       - 0.25 * metrics.equity.max_equity_drawdown
subject to metrics.all.num_trades >= 100
```

Best-so-far objective by equal trial budget:

| Trials | dlib global | native TPE | seeded random |
| ---: | ---: | ---: | ---: |
| 8 | -1475.23861375 | 296.31731625 | 296.31731625 |
| 16 | -324.40389625 | 296.31731625 | 296.31731625 |
| 32 | -105.22328125 | 1618.80984625 | 927.27561500 |
| 64 | 1521.64435500 | 1618.80984625 | 955.43960125 |
| 128 | 1727.29872000 | 1787.66159250 | 1189.46411250 |

The small-budget result is intentionally retained: dlib lost to random through
32 trials on this discontinuous surface, then overtook it by 64 trials. Adaptive
search is not a promise that every seed and budget beats random search.

The dlib best trial was:

```text
trial_id            = 116
ATR Length          = 28
Factor              = 6.667564806664945
net_profit          = 2179.739380000003
max_equity_drawdown = 1809.7626399999717
num_trades          = 1335
objective           = 1727.2987200000102
```

Repeating all 128 dlib trials with the same seed and four-worker batch produced
the exact same parameter/objective/status/metric projection. Its SHA-256 was
`9e07bb65a0c04161625f2e495506d1c94c9570d1549dd39444d2dabb3f7e1233`.

## Native TPE validation

The same compiled strategy, 1-minute corpus, objective, constraint, seed,
four-worker deterministic batch, and 128-trial budget were also run through the
native TPE sampler. Its configuration was:

```text
startup_trials = 10
ei_candidates  = 24
gamma_fraction = 0.1
gamma_cap      = 25
prior_weight   = 1.0
constant_liar  = true
```

All 128 trials completed with status `ok` and satisfied the trade-count
constraint. The best trial was:

```text
trial_id            = 105
ATR Length          = 48
Factor              = 6.8863211038926782
net_profit          = 2492.9676449999861
max_equity_drawdown = 2821.2242100000149
num_trades          = 1295
objective           = 1787.6615924999824
```

Every TPE trial reported the same engine aggregation evidence shown above:
3,334,340 input bars at 60 seconds became 222,295 script bars at 900 seconds,
with ratio 15 and `needs_aggregation=true`.

Two final direct native runs took 10.00 s and 8.95 s, or 12.80 and 14.30 trials
per second. Their complete result JSON files were byte-for-byte identical,
including all parameters, objectives, statuses, metrics, backtest metadata,
`schema_version=1`, `pineforge_hpo_version=0.1.0`, and
`sampler_implementation=pineforge_product_tpe_v1`. Both had SHA-256
`4083b7b67d001831b25aa3f4cba5c4f16dd00ebc8797869b99e41ccb4ed6f6eb`.

## Exploration allocation check

To test whether a strategy should always spend more of its budget on random
startup exploration, the same strategy study was rerun with five fixed seeds
and either 10 or 32 startup trials. Thirty-two is 25% of the 128-trial budget;
all other sampler, execution, objective, constraint, and data settings were
unchanged.

| Seed | Startup 10 best | Startup 32 best | Winner |
| ---: | ---: | ---: | ---: |
| 17 | 2052.878680 | 1869.444761 | 10 |
| 41 | 1667.607286 | 1072.011212 | 10 |
| 73 | 1880.846145 | 1659.958785 | 10 |
| 109 | 813.130771 | 1972.154523 | 32 |
| 149 | 1916.761325 | 1639.190457 | 10 |
| Median | 1880.846145 | 1659.958785 | 10 |

Startup 10 won four of five paired seeds. Every run produced 128 unique full
`(ATR Length, Factor)` pairs, but startup 32 covered a median 41 distinct ATR
values versus 35 for startup 10. Thus the larger allocation increased discrete
parameter coverage without improving median objective on this two-dimensional,
small-budget strategy problem.

All 1,280 trials were successful, feasible, satisfied the minimum-trade
constraint, reproduced the objective from report metrics, and reported the
same one-minute-to-15-minute aggregation contract. This comparison is quality
evidence, not a timing benchmark. Together with the million-candidate synthetic
result, it shows why exploration allocation should be compared per study rather
than hard-coded globally: a rugged six-dimensional space benefited from 20%
startup exploration, while this two-dimensional strategy benefited from earlier
model fitting.

## Wall-clock efficiency

Standalone cache-hit runs on an Apple M4 Max with 4 workers, including Python
orchestration, artifact validation, parsing the 168 MB CSV once, all backtests,
and JSON output, measured:

| Sampler | 128-trial wall time | Trials/second | Raw bar-trials/second |
| --- | ---: | ---: | ---: |
| dlib global | 9.06 s | 14.13 | 47.11 million |
| native TPE | 9.48 s | 13.51 | 45.04 million |
| seeded random | 7.21 s | 17.75 | 59.19 million |

dlib cost 25.7% and TPE cost 31.4% more wall time than random in these runs
because adaptive samplers update a model and wait at deterministic four-trial
batch boundaries. At 128 trials their best objectives were respectively 45.2%
and 50.3% higher than this random seed. Neither percentage should be generalized
without benchmarking the actual strategy, dimension count, worker count, and
budget.
