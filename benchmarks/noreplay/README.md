# No-replay continuation and exact full-history TPE

This suite isolates sampler import, the first new proposal, and an eight-proposal
constant-liar batch. It does not claim full job, JSON parsing, binary mapping, plugin
execution, or Cloud Run timings. No optimizer or storage dependency is added.

## Reproduce

Use a dedicated 8-vCPU Linux host, GCC, and an 8-GiB cgroup without swap. Compile
`profile.cpp` once against v0.6.0 (`6fc5b1fee4a2b76e5ee136f76805659460b9ad93`) and
once against this source. Retain both source trees; do not advance engine/codegen pins.
From the current source, with the baseline at `../baseline`:

```bash
mkdir -p build/noreplay
for variant in before after; do
  tree=.; flags=-ffp-contract=off
  if [ "$variant" = before ]; then
    tree=../baseline; define=-DPFH_NOREPLAY_BASELINE
  fi
  g++ -std=c++17 -O3 -ffp-contract=off -pthread $define -I"$tree/include" \
    benchmarks/noreplay/profile.cpp "$tree/src/core/types.cpp" \
    "$tree/src/core/search_space.cpp" "$tree/src/core/sampler.cpp" \
    "$tree/src/core/tpe_sampler.cpp" -o "build/noreplay/profile-$variant"
done
for phase in sequence prepare replay resume batch; do
  python3 benchmarks/noreplay/run_benchmark.py \
    --baseline build/noreplay/profile-before --native build/noreplay/profile-after \
    --source-revision "$(git rev-parse HEAD)" --phase "$phase" \
    --output build/noreplay/results
done
```

Every child runs foreground through `systemd-run` with `MemoryMax=8G` and
`MemorySwapMax=0`. Each phase emits a CSV, SHA-256 metadata sidecar and per-case
stdout/stderr. Never compare timing runs sharing their CPU host with other heavy work.
Single measurements are diagnostic, not a statistical performance guarantee.

## Fixtures and boundaries

- Seed 73, batch 8, default TPE configuration; `history_switch=1000` only in the
  explicitly bounded comparison. Five dimensions are continuous; 32 dimensions
  contain four integer grids and 28 continuous dimensions. Objectives tie modulo 997.
- `sequence` hashes typed integer values and exact binary64 suggestion bits across
  genuine fresh histories plus 16 additional suggestions. Full-history and bounded
  baselines must match the new implementation. `batch` separately compares the
  eight full-history suggestions at 100k, 500k and 2M rows, covering multicore fits.
- `prepare` generates genuine seeded 8k parent fixtures once. `replay` calls the old
  `warm_start(source, 8)` and measures import plus the first new ask/tell. The 1k/8k
  fixtures match uninterrupted proposals. Larger fixtures have the same matching
  8k prefix and a synthetic suffix; their deliberately short timeouts censor startup
  before the matching prefix ends. They are lower bounds on actual old import work,
  **not** completed 50k–1M uninterrupted-history replay measurements or extrapolations.
- `resume` imports the same parent fixtures, then measures one new ask/tell, with and
  without a checkpoint. Constructing a checkpoint parent is outside the import timer.
  Checkpoints in this timing fixture originate from direct import; exact restoration
  from genuinely uninterrupted parents is covered by native and runner oracle tests.
- `batch` imports deterministic permuted immutable columns before the timer and
  times eight cold-cache asks plus eight tells. It includes full-history model fitting,
  candidate acquisition and pending constant-liar updates, not just sorting. Bounded
  import/setup cost is excluded equally. OOM/timeout rows remain explicit in evidence.

## Exactness and migration

Old terminal rows do not record the number of model-dependent numeric rejection
draws, reservation retries, or losing acquisition candidates. A fixed RNG discard
cannot recover the legacy cursor. v0.7.0 therefore never replays proposals and restores
exactness through a saved RNG/reservoir/cache checkpoint; legacy row-only TPE parents
reconstruct deterministically. Grid/random continuation is unchanged.

The exact optimization parallelizes dimensions, not sampling or reductions. Each
dimension keeps its original row order, kernel construction and arithmetic; candidate
sampling stays serial and density contributions reduce in declaration order. Maintaining
incremental sorted rows alone cannot eliminate exact kernel refresh: inserted observations
change neighbor bandwidths, chronological weights, good/bad membership and pending
constant-liar components. Bounded mode is a different estimator and remains an owner
decision. See `docs/study-spec.md` for the checkpoint wire contract.

## Measured pre-review result (2026-10-04 UTC)

One foreground run on AWS spot `c7i.2xlarge`, eight vCPUs, shaped to 8 GiB with no
swap; GCC 13.3.0, `-O3 -ffp-contract=off -pthread`. These are sampler-only seconds,
not Cloud Run estimates. Before is the actual v0.6.0 implementation; after uses the
pre-review PR #9 sampler source at `633a73e1`. See the boundaries above before comparing these numbers
with application startup or isolated model-fit measurements.

### Time to first new trial

| Inputs | Parent trials | Before: replay | After: row-only rebuild | After: checkpoint |
| ---: | ---: | ---: | ---: | ---: |
| 5 | 1,000 | 0.817078 | 0.005151 | 0.005190 |
| 5 | 8,000 | 59.299945 | 0.033790 | 0.033796 |
| 5 | 50,000 | >29* | 0.206365 | 0.206833 |
| 5 | 500,000 | >29* | 2.136493 | 2.133383 |
| 5 | 1,000,000 | >29* | 4.291056 | 4.292948 |
| 32 | 1,000 | 6.960174 | 0.021780 | 0.022058 |
| 32 | 8,000 | 461.686966 | 0.083636 | 0.084118 |
| 32 | 50,000 | >60* | 0.508033 | 0.499783 |
| 32 | 500,000 | >60* | 5.095935 | 5.098186 |
| 32 | 1,000,000 | >60* | 10.316139 | 10.268524 |

`>` means an observed timeout, not an extrapolated completed duration. `*` marks
the large matching-prefix/synthetic-suffix fixtures: the old sampler had not finished
even the matching 8k prefix at the timeout. Those rows prove a lower bound only;
they do not measure a complete matching 50k–1M legacy uninterrupted history.
The genuinely uninterrupted 32-input 8k fixture initially hit its 300-second limit;
an extended foreground run completes in 461.686966 seconds, as shown in the table.
After import performs two linear parameter passes, with no historical `ask()` calls.
Checkpoint timing parents are built by direct import outside the timer; independent
oracle tests establish exact uninterrupted-stream restoration.

### Eight-proposal batch

| Inputs | History | Before: full | After: exact full | After: bounded switch 1,000 |
| ---: | ---: | ---: | ---: | ---: |
| 5 | 100,000 | 1.556609 | 0.802381 | 0.001010 |
| 5 | 500,000 | 9.571853 | 4.523221 | 0.001008 |
| 5 | 2,000,000 | 41.256074 | 19.811430 | 0.001087 |
| 32 | 100,000 | 12.620050 | 2.762587 | 0.004524 |
| 32 | 500,000 | 65.431925 | 14.433096 | 0.004490 |
| 32 | 2,000,000 | 271.076857 | 62.685604 | 0.004568 |

All six full-history batch suggestion hashes match the actual baseline bit-for-bit.
The 500k/32-input batch improves 4.53x; the 2M/32-input batch improves 4.32x. Bounded
numbers are deliberately separate: they describe a different estimator and exclude
the history import/setup cost equally with the full-history rows. No default changes.

The CSVs and SHA-256 metadata sidecars are retained under ignored
`build/noreplay/results/`. Published table inputs are `replay.csv`,
`replay-complete/replay-32-8000.csv`, `resume.csv`, the baseline rows of `batch.csv`,
and the final-source rows of `batch-native.csv`.
The initial `sequence.csv` covers genuine 128/4,104 full-history and 256-row bounded
parents at both input counts; `sequence-native.csv` and runner replay-oracle checks
also verify the release implementation. Raw ad-hoc artifacts are not committed.

### Pre-review verification gates

- Release CTest: 30/30; Python: 70/70 on the unshipped pre-review implementation.
- ASan/UBSan: 30/30, leak detection enabled, 32-MiB quarantine. The initial default
  256-MiB quarantine trips the existing 64-MiB RSS-growth assertion; the documented
  quarantine setting resolves it without disabling or loosening any assertion.
- TSan: 30/30, including the 4,200-row parallel warm-start test. The disposable
  host uses mmap randomization 28 for TSan and restores its original value 32.
- Actual v0.5.0 and v0.6.0 replay oracles: 88 cases each, 3,712 new suggestions
  each. Checkpoint JSON, single binary blocks and reversed binary chunks match;
  grid/random and row-only reconstruction cases preserve their prior behavior.
- Final-binary sequence checks: all six full/bounded parent traces match baseline;
  all six large full-history batch suggestion hashes also match bit-for-bit.
- Real strategy: one compiled artifact, 12 continuation cases at batches 2/5/8,
  and a canonical fixed candidate with seven identical C-ABI trade records and
  four identical recorded metrics. No engine/codegen gitlink changes.
- Ruff 0.15.20: check and format check pass. Clean Doxygen: zero warnings;
  generated-site validation: 247 HTML files. Pinned Optuna smoke: six CSV rows,
  with verified CSV digest and retained metadata sidecar.

## Reviewed 0.7.0 result (2026-10-04 UTC)

Sampler source `46bb7d20281c4dc4daaabad55d96e6ecf266963b` replaces the unshipped
checkpoint with canonical `PFHTPE2`, enforces numerical-build identity and uses
persistent quota-aware workers. These changes warrant refreshing the cost tables.
The new measurements run alone on spot instance `i-04305f7dbad41c695`, an
8-vCPU `c7i.2xlarge` with an 8-GiB/no-swap cgroup (the physical instance has
16 GiB), GCC 13.3.0, `-O3 -ffp-contract=off -pthread`, seed 73 and batch eight.

### Time to first new proposal

Seconds for sampler import plus the first new ask/tell. The before column retains
the earlier actual v0.6.0 replay measurements on the same resource shape; both
after columns are refreshed. File parsing, plugin/data loading and strategy
execution are outside this timer. The fixture and censoring boundaries above apply.

| Inputs | Parent trials | Before: replay | After: row-only | After: checkpoint |
| ---: | ---: | ---: | ---: | ---: |
| 5 | 1,000 | 0.817078 | 0.005472 | 0.005437 |
| 5 | 8,000 | 59.299945 | 0.038097 | 0.036429 |
| 5 | 50,000 | >29* | 0.214840 | 0.221478 |
| 5 | 500,000 | >29* | 2.245702 | 2.245443 |
| 5 | 1,000,000 | >29* | 4.563063 | 4.535660 |
| 32 | 1,000 | 6.960174 | 0.023607 | 0.022851 |
| 32 | 8,000 | 461.686966 | 0.087531 | 0.087696 |
| 32 | 50,000 | >60* | 0.537085 | 0.537704 |
| 32 | 500,000 | >60* | 5.533129 | 5.539204 |
| 32 | 1,000,000 | >60* | 11.161494 | 11.158025 |

`*` denotes a censored matching-8k-prefix/synthetic-suffix fixture, **not** a
completed matching 50k–1M uninterrupted-history replay. Checkpoint timing parents
are constructed by direct import outside the timer; genuine uninterrupted-parent
exactness is established by the independent executable oracles, not these timers.

### Eight-proposal batch

All three columns are remeasured. Seconds include cold-cache fitting, acquisition
and pending constant-liar updates, excluding import/setup equally across modes.

| Inputs | History | Before: full | After: exact full | Bounded: switch 1,000 |
| ---: | ---: | ---: | ---: | ---: |
| 5 | 100,000 | 1.652692 | 0.847239 | 0.001044 |
| 5 | 500,000 | 10.195724 | 4.856719 | 0.001035 |
| 5 | 2,000,000 | 43.988586 | 21.283209 | 0.001153 |
| 32 | 100,000 | 13.401302 | 3.161808 | 0.004764 |
| 32 | 500,000 | 69.167101 | 16.534632 | 0.004770 |
| 32 | 2,000,000 | 287.644111 | 70.008436 | 0.004720 |

All six exact-full suggestion hashes equal the unmodified v0.6.0 baseline. The
500k/32-input batch improves 4.18x; the 2M/32-input batch improves 4.11x. At 2M/32,
peak RSS is 7,738,616 KiB before and 4,936,308 KiB after. Full-history work still
grows with history. These single measurements are not Cloud Run timings or an SLA.
Bounded mode remains a different model and an owner decision; no default changes.

### Reservoir mutation evidence

Native cases use exactly 201 parent observations plus 300 new observations,
`history_switch=32`, `bad_reservoir_size=7`, and the listed batches. Each mutant is
compiled separately from the final sampler source. Both fail with
`uninterrupted and continuation proposals differ` rather than a setup error.

| Implementation | Batch 1 | Batch 5 | Batch 8 |
| --- | --- | --- | --- |
| Control | PASS (exit 0) | PASS (exit 0) | PASS (exit 0) |
| One extra reservoir draw after restore | KILLED (exit 1) | KILLED (exit 1) | KILLED (exit 1) |
| Skip reservoir RNG restoration | KILLED (exit 1) | KILLED (exit 1) | KILLED (exit 1) |

### Reviewed verification gates

- Release CTest: 31/31; ASan/UBSan: 31/31 with leak detection and a 32-MiB
  quarantine; TSan: 31/31 with temporary mmap randomization 28, reset to 32 afterward.
- Python: 70/70, installed package 0.7.0. Ruff 0.15.20 check and format check pass.
- Actual v0.5.0 and v0.6.0 runner oracles: 91 cases and 4,612 new suggestions each,
  including three small-reservoir bounded checkpoints and successful foreign-build
  and shorter-history reconstruction. Old replay parents are rounded to complete
  batches (201/205/208); native mutation cases deliberately retain a 201-row parent.
- The checked-in canonical two-engine golden restores the same 4,096 future words
  per engine on Ubuntu/libstdc++ and macOS/libc++ CI. Its shared SHA-256 is
  `3132b2ec79fbb1b9741d1c771356ba94ffce7e41c5e172e442ddc5350fb6db70`.
  Actual Mac-to-Linux and Linux-to-Mac full checkpoint exchanges rebuild with exit
  zero because numerical builds differ; same-build checkpoints restore.
- Explicit one/eight-worker tests cover 4,200 mixed/log rows and 4,096 rows with
  32 inputs, including independent v0.6.0 serial hashes. Worker reuse, resource-failure
  fallback, quota parsing, and a real 150% CPU-quota container are tested.
- Real-strategy E2E: 12 continuation cases, one compiled artifact, seven identical
  canonical C-ABI trade records and four identical metrics. Gitlinks remain pinned.
- Doxygen: zero warnings; 247 HTML files validated. Pinned Optuna smoke: six CSV
  rows with a metadata sidecar. Both native CI platforms pass.

Raw review evidence is retained under ignored `build/review/`. Refreshed table
inputs are `timings/resume-native.csv` and `timings/batch.csv`; their metadata
sidecars include verified CSV, fixture, executable and current-source hashes.
The smoke sidecar, `oracle-v050-equivalence.json`, `oracle-v060-equivalence.json`,
`mutant-results.json`, exchange logs and gate logs are retained alongside them.
