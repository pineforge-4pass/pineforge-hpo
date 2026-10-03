# Native scaling in 0.4.0

**Release gate blocked:** the [review-fix measurements](review-2026-10-03.md)
supersede the estimator, acquisition, and quality claims below. The tables below
describe the original PR implementation, not the reservoir implementation. Its
100/300/1,000-trial comparison is an identity check, not a quality gate.

Measurements use one dedicated AWS 8-vCPU, 16-GiB Linux machine, Release C++17,
and baseline `6ccb6d4` (0.3.0). Engine and codegen gitlinks remain pinned.
No local workstation or other compute host is used for these gates.
Small, reviewed CSV evidence and SHA-256 metadata live in
[`../optuna/evidence/`](../optuna/evidence/README.md), named
`2026-10-03-scale-{ask,native,quality}.csv` and `.csv.metadata.json`.

## Acquisition versus history

Prefill with seeded random sphere observations, then time acquisition separately
from objective execution. Baseline uses snapshot asks without feedback; after
uses 256 asks with ordered batch-eight feedback, including pending-liar work
and periodic model refits. The million-history baseline is a probe, not a full
million-trial adaptive study. Times are microseconds per ask, not seconds.

| Dims | Before 1k | Before 10k | Before 100k | Before 1M | After 1k | After 10k | After 100k | After 1M |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | 6,687 | 67,294 | 693,962 | 7,164,921 | 17.37 | 16.84 | 16.05 | 16.11 |
| 8 | 13,350 | 134,385 | 1,390,416 | 14,243,296 | 30.41 | 30.44 | 29.26 | 29.23 |
| 12 | 20,979 | 202,615 | 2,113,361 | 22,646,468 | 44.68 | 45.47 | 42.50 | 42.44 |
| 16 | 26,999 | 280,271 | 2,870,965 | 29,058,136 | 58.94 | 59.76 | 56.96 | 56.71 |
| 20 | 33,954 | 342,106 | 3,631,701 | 36,664,225 | 73.94 | 74.35 | 70.69 | 71.17 |
| 32 | 54,727 | 555,396 | 6,005,052 | 60,482,184 | 122.14 | 123.42 | 117.53 | 117.13 |
| 64 | 111,827 | 1,141,206 | 12,740,957 | 130,175,078 | 256.46 | 257.23 | 246.70 | 244.55 |

The after sampler retains 89 observations at every measured history length.
At 1M/64D its isolated peak RSS is 10.62 MiB versus 8.23 GiB before. The warm-up
still uses the full-history estimator; the table measures the scaled regime.
The actual 100,000-trial adaptive regression, not just prefill, asserts retained
history, bounded RSS growth, early/late ask time, and reset replay.

## Chosen estimator

The default first 1,000 proposals preserve legacy acquisition. Thereafter retain
the global best 25 observations plus 64 recent non-elites, rather than an
unbounded bad mixture. Cache both models in deterministic 32-completion epochs;
retain live pending-liar overlays. Precompute mixture constants, use 513-point
interpolated numeric density tables, and reduce EI draws from 24 to eight.
Tiny discrete bins use midpoint density; ordinary bins retain mass evaluation.
The enlarged gamma-cap configuration extends warm-up as documented in the API.

An intermediate bounded implementation measured 383 microseconds at 1M/64D with
batch-eight updates; caching both models and normalized pending coordinates
reduces that to 245. No parallel scorer or SIMD dependency is needed. These
figures are acquisition measurements, not a statistical ranking of algorithms.

The paired pinned HPO-BENCH replica covers 38 problems, instance 1, seeds
17/48/79, batch8/lag0, at 100/300/1,000 trials. The 114 paired studies have
maximum absolute normalized-regret difference zero at all three budgets.
Median regrets are 0.2826473, 0.1271432, and 0.0773732 respectively, identical
before and after. The first 1,000-proposal preservation is intentional.
This is not evidence of long-budget quality equivalence. The hard-suite
replica and normalizers are hashed in the quality sidecar; COCO uses
`numbbo/coco-experiment` revision `dd4bd1f0cc7699a2b612448d85aafb94636fe947`.

A 32D extension adds BBOB f15–f24 with the same three seeds (30 paired studies).
Its median normalized regrets at 100/300/1,000 trials are
0.3672532/0.2529880/0.1221223 before and after, again with maximum paired delta
zero. Together the checks span 2–32 dimensions and 144 paired studies.

## Native throughput, barriers, and billing

At W=8, batch8/lag1, 64 continuous inputs, a 3-ms synthetic plugin objective,
and `--trials-out none --best-k 10`, 1,000,000 terminal trials take 490.75 s
(2,038 trials/s), with peak RSS 20.78 MiB and 22,860-byte final JSON.
All 1,000,000 billing lines are strict-parsed, unique, monotonic, and complete.
Their unchanged schema occupies 2,145,854,315 bytes. Serialization costs
53.55 microseconds/line; pipe writing costs 4.03 microseconds/line. Final JSON
rendering takes 0.474 ms and writing 0.134 ms. This includes the legacy warm-up.
The fd stream is drained concurrently; a slow reader applies backpressure.

The real strategy is `strategy.pine` (EMA/RSI/ATR entries, stops and targets),
compiled once, with deterministic synthetic OHLCV. Each shape uses 1,000 trials.
Baseline has no direct proposal/barrier counters, so its worker idle time is
reported separately rather than mislabeled as barrier time.

| Shape/mode | Native wall s | Worker idle s | Proposal s | Barrier s |
| --- | ---: | ---: | ---: | ---: |
| 4 inputs, 744 hourly bars, before | 8.35 | 61.93 | — | — |
| Same, after lag0 | 5.66 | 40.42 | 5.05 | 0.60 |
| Same, after lag1 | 5.46 | 38.00 | 5.39 | 0.059 |
| 12 inputs, 35,064 hourly bars, before | 66.10 | 151.14 | — | — |
| Same, after lag0 | 60.08 | 71.73 | 8.82 | 51.17 |
| Same, after lag1 | 53.81 | 2.33 | 17.71 | 35.98 |

Lag1 improves wall time by 34.6%/18.6% versus baseline on these two shapes;
it changes feedback lag and therefore the proposal sequence. Timing samples
are single runs, not confidence intervals. These synthetic-bar results are
not claimed to reproduce the app's exact production timings.

The slow shape uses 2,103,840 one-minute bars (four years), 12 inputs, and six
triangle magnifier samples. Sixteen trials take 54.03 s at lag0 and 53.40 s at
lag1, with peak RSS 2.45/2.42 GiB. Memory there is dominated by bars/engine
workers, not retained trial history. A million such backtests is not promised
within an hour: the million-trial gate is the lightweight 3-ms shape.

## Retention and reproduction

`all` remains default and intentionally scales with trial count. Both bounded
modes retain only best-k records and scalar status/coverage, with a bounded
progress queue. Exact sparse finite coverage uses temporary disk storage; it
does not grow resident sets. Enabled pruning retains 1,024 scores per rung.
The output-only matrix uses random sampling to isolate payload/JSON cost from
acquisition, at 1k/10k/100k/1M trials and all/best-k/none retention modes.
The CSV contains final JSON bytes/render/write time and peak RSS for every cell.

At one million trials, compatibility `all` produces 606,780,201 bytes, uses
2.41 GiB peak RSS, and spends 8.470 s rendering plus 3.767 s writing final JSON.
The bounded `none` run produces 7,283 bytes, uses 4.52 MiB peak RSS, and spends
0.092 ms rendering plus 0.117 ms writing; its full billing stream is still
601,779,179 bytes. `best-k` produces 13,313 bytes. The `all` matrix disables
progress to isolate archival output; bounded-mode timings include a drained
progress pipe and therefore are not end-to-end speed comparisons with `all`.

## Gate output

All builds and execution gates run on the same owned AWS Linux machine:

- Release: 26/26 tests passed (71.42 s), including 100k-trial bounded-memory/time,
  worker-count replay, pruning, deadline/SIGTERM complete lines, and output parity.
- Python: 45 tests passed.
- ASan/UBSan: 23/23 passed (36.33 s); the quality, stress, and signal macro suites
  run in Release, not under ASan. Leak checking is enabled with a 16-MiB quarantine.
- TSan: 4/4 passed (37.68 s): batching, executor, output contracts, and concurrency.
- Pinned quality scheduler parity: 1/1 passed; 144 paired quality studies complete.
- Doxygen: zero warnings; generated-site validator checks 219 clean HTML files.
- Documented Optuna smoke: completed with pinned requirements and metadata sidecar.

The final million-trial gate repeats the same billing SHA-256 as the earlier run:
`32e20bb51044de394b9cb4417300de9cbd3523dc433d77633e03dc13bc4fbc30`.
Bounded resident-history claims apply to TPE/grid/random and bounded output modes;
compatibility `all`, dlib's own optimizer history, immutable bars, and engine
working memory are explicit exclusions. Native `prepare` remains a separately
gated 0.4.x follow-up.

Run on an isolated Linux benchmark machine with the pinned engine/codegen:

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j8
g++ -O3 -DNDEBUG -std=c++17 -Iinclude benchmarks/scaling/profile.cpp \
  build/lib/libpineforge_hpo_core.a -lpthread -o build/scale-profile
for dims in 4 8 12 16 20 32 64; do
  for history in 1000 10000 100000 1000000; do
    build/scale-profile "$dims" "$history" 256 update
  done
done
PYTHONPATH=python:external/pineforge-codegen-oss python3 \
  benchmarks/scaling/run_native.py --native build/bin/pineforge-hpo-native \
  --fixture build/lib/fake_strategy.so --output build/scaling/million \
  --dims 64 --trials 1000000 --delay-ms 3
```

For a baseline snapshot, compile the same probe with
`-DPINEFORGE_HPO_LEGACY_TPE` against the baseline core and use one repetition
without `update` for the very large histories. Use `run_native.py --real` for
the hourly shapes, `--real --slow` for the magnifier shape, and
`--sampler random --delay-ms 0 --modes all best-k none` for output isolation.
Provide `--engine-root` and `--baseline` when needed. Preparation is excluded
from native wall time. `elapsed_seconds` also includes driver JSON parsing;
`wall_seconds` is the native process measurement. `quality_replica.py` accepts
the separately supplied pinned harness binaries and normalizers; it hashes
the replica sources and exact binaries with every paired run.

Native/ASan/UBSan/TSan, Python, zero-warning Doxygen/site validation, and the
documented Optuna smoke profile are release gates. Their recorded output is
included in the PR. Native `prepare` is deliberately left for 0.4.x after these
phases; it is not silently implemented as part of the trial loop.
