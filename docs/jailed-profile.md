# Running in the engine's jailed scratch profile

REFUSAL FIX, not a feature: hpo trials refuse in the engine's jailed profile; this makes the existing engine C ABI path reachable.

Engine 1.7.0 supplies an inherited scratch arena and bounded-memory historical
tables for hosts whose engine scratch cannot use writable disk. The external
jailer creates the arena, establishes filesystem restrictions, and supplies
readable strategy artifacts and OHLCV data. HPO consumes that existing C ABI;
it does not create the jail or the arena. Compile the strategy artifact before
entering the jail.

## Environment and descriptors

Presence of `PF_SCRATCH_ARENA_FD` selects this path, including an empty or malformed
value. The engine validates the values and backing; Python only preserves fd 5
when it launches the native runner.

| Input | Contract |
| --- | --- |
| fd 5 | Inherited readable/writable arena, retained at its supplied size until the native child exits. |
| `PF_SCRATCH_ARENA_FD` | Positive decimal integer selecting exactly fd 5; normally `5`. |
| `PF_SCRATCH_ARENA_BYTES` | Required positive decimal byte count, exactly equal to the backing size. At least one complete 1 MiB chunk must fit. |
| `PF_SCRATCH_WINDOW_BYTES` | Optional positive decimal cap on the process's mapped arena windows. The engine defaults to 32 MiB and rounds down to whole pages; less than one page is refused. This is not the arena's capacity. |

On Linux, admission requires a sealed memfd with
`F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL`. Additional non-write seals are
accepted. `F_SEAL_WRITE` and `F_SEAL_FUTURE_WRITE` are refused because the engine
needs writable shared mappings. Ordinary files and sealing-disabled memfds do
not satisfy Linux jailed admission. These admission checks apply regardless of
the optional engine variable `PF_SCRATCH_ARENA_JAILED`. Off Linux the engine
admits an attached ordinary-file arena; the real sealed-memfd tests explicitly
skip there.

An existing `--progress-fd` is preserved alongside fd 5. `--progress-fd 5` with
the arena variable present refuses before spawning with `hpo_cli_usage` and
empty arguments. HPO neither opens, validates, resizes nor substitutes arena
backing in Python.

With the arena variable present and `--output` given, Python also passes `--output`
to the native runner, so the native trial spool can use the output's parent
directory. The native runner therefore writes the result file first; Python then
replaces it atomically with the final document after adding `study`, `strategy_id`,
`dataset_id` and `artifact`. If Python fails in between (for example on invalid
native JSON), the file holds the native document without those four fields.

## Startup and fresh handles

The native executor creates a validation handle on the main thread before
starting workers or a batch (one extra create and free per study, only with the
variable present). The engine would otherwise attach the arena lazily, from the
first report on whichever worker thread gets there first, and the first attach is
not thread-safe. With the arena variable present it installs the
all-zero version-1 bounded-memory tables descriptor, then calls
`strategy_require_report_scratch_arena_v1`. No trial settings or execution are
applied to this validation handle. A failure propagates with its engine code
and arguments, exits 1, and publishes no result file.

Every fresh trial handle, including prefix trials and selected-window
evaluations, installs the same tables descriptor and requests arena admission
after settings and symbol feeds and before execution. The engine expands the
all-zero maxima to its default P0 policy once during common run preparation.
For a finite batch its successor-root allowance is twice the number of fed
input bars; this policy is an admission limit, not a proof that every strategy
fits. Each handle gets fresh table occupancy.

The first successful arena attachment sets **SIGXFSZ to SIG_IGN process-wide**.
Detaching does not restore the previous disposition. The jailer must account
for this signal policy, including installing its required disposition before
applying file-size restrictions.

The arena is a process-wide engine singleton. Reattaching the same fd and size
is an idempotent no-op; replacing a live attachment is refused. Startup
preflight makes the first attachment before worker threads exist. Per-handle
admission reuses that attachment and only reads the singleton: no production
engine code detaches the arena, so that read cannot race a write. HPO keeps no
attachment flag of its own.
When `PF_SCRATCH_ARENA_FD` is absent, HPO creates no extra validation handle and
calls neither new export, including when using older plugins without them.

## Sizing for the jailer

All `--workers` threads in one native process share **one arena capacity**.
The engine claims regions in 1 MiB chunks under a mutex. There is no per-trial
partition: scheduling decides which trial reaches the shared bound. Size for
the sum of simultaneously live scratch demands, with each region's chunk
rounding, rather than assigning the arena's entire capacity independently to
each worker.

The engine's scratch-tables contract states: “Concurrent HPO trials charge the
sum of their slabs; retiring readers can keep a preceding slab alive too.”
It also states: “The wrapper must charge the arena as well as the table slab.”
The P0 slab's root region adds about 32 bytes per fed bar, plus its separately
rounded fixed regions. Report/ledger and command scratch use the separately
charged arena. OHLCV, strategy heap, report arrays, metadata and allocator
overhead are additional. `PF_SCRATCH_WINDOW_BYTES` bounds mappings, not claimed
chunks or these other allocations.

The engine's arena-bound design uses pre-start strategy/data facts and explicitly
distinguishes a certified finite bound from an unknown or unbounded term. HPO
does not supply those facts or a new universal bytes-per-trial formula. The
jailer owns the memory entitlement and arena size for the admitted strategy,
input and worker concurrency.

## Refusals

| Engine code | Arguments and outcome |
| --- | --- |
| `report_sink_failed` | `reason: arena_backing` for invalid/missing arena configuration or unsuitable backing. Native startup refuses before execution. |
| `strategy_library_incompatible` | `reason: setter_missing` for an absent tables setter, or `reason: symbol_missing` for absent arena admission. Startup refuses before execution. |
| `scratch_capacity_exceeded` | The engine's `resource` identifies an exhausted bounded table. It remains an engine-error trial and cannot rank. |
| `report_sink_failed` | `reason: arena_exhausted` with the engine's capacity diagnostics when the shared arena or a store's arena allowance is exhausted during a trial. It remains an engine-error trial and cannot rank. |

Engine codes and argument JSON are preserved. Per-trial failures keep the
existing study exit/publication policy; they do not become startup refusals or
trigger disk fallback.

## HPO scratch that is not covered

An ordinary study defaults to retaining all trials and **still opens two
unlinked spool files**. Supply `--output` in a real writable directory. In the
jailed Python path this existing output option is also passed to native so
its spool can use that parent directory. A memory-backed writable scratch
filesystem is sufficient for the spool and result. Without a writable output
parent, native selects a non-empty `TMPDIR`, otherwise `/tmp`.

Finite-space coverage above cardinality 100,000,000, and finite warm-history
coverage using the file-backed ordinal index, can also require `std::tmpfile`.
This fix does not remove either HPO storage path, make array reports bounded,
or establish a study with no writable filesystem or `RLIMIT_FSIZE=0`.

The Linux real-plugin gate uses a writable output directory and a `TMPDIR`
that does not exist. Engine file fallback therefore refuses instead of
quietly writing there. It compares three trials with the same live unjailed
study and reads native arena high-water diagnostics after a real trial.
A directory scan would not prove absence of the unlinked HPO spool files.
