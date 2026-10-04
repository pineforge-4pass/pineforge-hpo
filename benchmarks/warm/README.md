# Binary warm-start resources and equivalence

This suite compares the actual v0.5.0 native runner at
`df7f61300fbb9222f20022127ec0058cc0c9c8d1` with v0.6.0. It is a format-loading
benchmark, not an optimizer-quality, complete-job or storage-throughput claim.
Raw evidence and CSV metadata stay under ignored `build/` directories.

The historical v0.6.0 result measured format loading only. Since v0.6.1, row-only TPE
parents reconstruct without proposal replay; exact continuation requires the optional
sampler checkpoint. The [no-replay suite](../noreplay/README.md) measures import through
the first new trial, tests checkpoint continuation against the old replay oracle, and
compares exact full-history versus opted-in bounded batch cost.

## Resource protocol

Use an otherwise idle little-endian Linux machine with 8 vCPU. Each measured
process runs in its own cgroup with `MemoryMax=8G` and `MemorySwapMax=0`; this
includes file-backed pages charged to that cgroup. The acceptance host has 16 GiB
physically, with this enforced 8-GiB process budget. GNU time records process RSS
and elapsed time. Generation and file hashing are outside the measured boundary.

```bash
python3 benchmarks/warm/run_benchmark.py \
  --baseline build/baseline/bin/pineforge-hpo-native \
  --native build/bin/pineforge-hpo-native \
  --output build/warm-resources
```

The driver generates exact unique deterministic permuted histories at
100k/500k/1M/2M rows and 5/32 inputs. Five-input histories use float64 columns;
32-input histories use four int32 grids plus 28 float64 columns. All objectives
are finite with deliberate ties. The equivalent JSON includes three report metrics.
One cold-page-cache run (`drop_caches`) and three subsequent warm-cache runs are
retained for each format/size. It requires sudo for transient cgroups and cache
eviction. Failed loads retain their actual error/time/RSS; they are never presented
as successful load timings. v0.5.0 refuses documents above 256 MiB, independently
of available memory.

The measured command is native `space-info --spec ... --warm-start ...`: file
read/mmap, all scalar/schema/hash validation and an exact tried-vector index.
It does not load a strategy/data, replay historical TPE proposals, fit density
models or render ancestor JSON. Binary payload SHA-256 is deferred; the unmodified
v0.5 JSON loader includes its original digest computation. Ingest/download/object-hash
work normally precedes this loader and populates the OS page cache. Cold times
also measure the provisioned disk and can exceed the subsecond warm-load target.

`resources.csv.metadata.json` includes the fixture seed, memory budget, versions,
baseline revision, compiler flags, executable SHA-256, every generated object hash,
CSV hash and measured boundary. Preserve it alongside `resources.csv`.
For a copied build tree without Git metadata, pass `--source-revision FULL_COMMIT`
to bind the measurement to the reviewed source revision.

## Bitwise continuation gate

```bash
python3 tests/test_warm_binary_contract.py \
  build/bin/pineforge-hpo-native build/lib/fake_strategy.so \
  --baseline build/baseline/bin/pineforge-hpo-native \
  --output build/warm-equivalence
```

The gate compares packed uint64 IDs and binary64 parameter/objective bits, states
and typed categorical values, not approximate float equality. It covers grid,
random, TPE and constrained TPE, finite/mixed/log spaces, and batches 1/4/8/32.
Each v0.5 parent feeds v0.5 JSON, v0.6 JSON, one binary block and reversed chunks.
The native converter and Python writer must match bytes. The JSON child of a
binary parent must preserve its full ancestry for a grandchild. Evidence includes
per-case suggestion and warm-object SHA-256 in `equivalence.json`.

The native golden/corruption tests validate every truncation point, versions,
counts, flags, descriptors, states, indices, duplicate IDs and wide IDs. Python
goldens validate the same writer bytes. The real-strategy gate also runs binary
and multi-block continuations against JSON, with canonical harness metric/trade
comparison:

```bash
python3 tests/test_warm_start_e2e.py \
  --native build/bin/pineforge-hpo-native --output build/warm-real-strategy
```

Run Release, ASan/UBSan and TSan builds as documented in the root build options,
the full Python suite, the pinned Optuna smoke profile in `benchmarks/README.md`,
and the zero-warning Doxygen/generated-site gates before release claims.
