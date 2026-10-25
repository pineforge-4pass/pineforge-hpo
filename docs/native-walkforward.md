# Native walk-forward

## Planner helper loader

`WindowPlanHelper::load(absolute_path, expected_sha256_hex, private_run_directory)` loads the
standalone `libpineforge_window_plan` companion. The caller supplies its trusted absolute source
path, the expected 64-character lowercase SHA-256, and an existing absolute run directory owned
by the effective user with no group or other permissions. Relative source paths are refused
before normalization or filesystem access. There is no implicit path search or temporary-directory
fallback. A worker owns one helper and reuses it for its planning calls.
The loader requires that every ancestor of the run directory is root- or user-owned and not
writable by others unless sticky.

The loader opens the source read-only with close-on-exec and `O_NONBLOCK`, so opening a FIFO
never waits for a writer. It requires a regular file and copies from that held descriptor into
a new randomly named, exclusive snapshot created with mode 0600.
It streams SHA-256 over the copied bytes and checks the source descriptor's device, inode, size,
and full mtime/ctime before and after copying. It refuses changed sources with
`hpo_walkforward_unavailable{reason:window_plan_changed}` and unequal content with
`reason:window_plan_hash_mismatch`, before loading any helper code. It then makes the snapshot
0400, closes its write descriptor, checks the snapshot path identity, and calls `dlopen` only on
the absolute snapshot path with `RTLD_NOW | RTLD_LOCAL`. Source symlinks are followed once by
`open`; a later source path or symlink replacement cannot redirect the snapshot copy.
The helper retains the opened source's device, inode and byte length, its verified SHA-256,
and the actual snapshot path for runtime provenance.
`window_plan_changed` is safe to retry. A `noexec` run directory fails `dlopen` with
`reason:window_plan_load`.

The helper binds `pf_selected_plan_version` and `pf_plan_selected_primary_v1`, requires version 1,
and executes the frozen `selected-plan-utc-six-hours/v1` self-test once before returning. Every
expected field is a literal from the engine v1.7.0 fixture; the loader does not obtain expected
values from the helper or a manifest. A later engine planner behavior change must bump this
self-test identifier and its frozen expectations. Hash equality and this one fixture establish
content identity and a behavioral check; stale helpers that share this behavior still require
controlled archive/helper build provenance and the real-plugin gates.

`PlanRequest` owns the five strings and uses the engine's fixed-width T/E/F/N fields.
`plan_selected_primary(rows, count, request)` passes the caller's `const pf_bar_t*` unchanged,
borrows the rows and strings for the call, and returns all 248 bytes in `PlanResult::raw`.
`PlanStatus::Ok` is distinct from the planner's six refusal statuses, which remain result data.
The C bridge's negative returns (-1 invalid call, -2 allocation failure, -3 bridge invariant)
throw typed runtime errors with `reason:window_plan_load`. Every returned descriptor must have
the exact size, version 1, reserved 0, valid status/bound/mask/Boolean words, zero absent
timestamps, and a NUL-terminated, zero-padded ASCII option. Malformed results also use
`window_plan_load`; a failure during the load-time golden check uses `window_plan_self_test`.
Missing exports have separate `window_plan_missing_pf_selected_plan_version` and
`window_plan_missing_pf_plan_selected_primary_v1` reasons; a mismatched probe uses
`window_plan_version`, and a relative source uses `window_plan_path`.

Keep the helper alive until all calls finish. Its destructor calls `dlclose` and never deletes
a file. The run-directory owner keeps the directory ancestry stable and all snapshots untouched
for the worker lifetime, then owns cleanup of successful and failed-load snapshots.
Snapshot files left after a hash mismatch contain **UNVERIFIED** bytes and are the run-directory
owner's to clean up. Snapshot storage is one helper copy per worker, independent of the number
of planner calls. The loader does not execute a strategy, admit request-feed inventories, or
implement selected execution.

The `pineforge_hpo_window_plan` CTest test reads the real engine helper from
`PFH_WINDOW_PLAN_LIB`. It reports `UNAVAILABLE` when the helper is unset or missing, and that
condition fails when `PFH_REQUIRE_WINDOW_PLAN=1`. Non-PR native CI builds the engine target
`pineforge_window_plan`, requires its exact `build/lib/libpineforge_window_plan.so` (Linux) or
`.dylib` (macOS) output, and enables the required mode for CTest. PR core CI does not compile
the adapter. Real helper vectors cover the golden fixture, request and boundary refusals,
unordered direct arrays, feed range, clock/pairing, pending pre-roll completion, source swaps,
in-place growth and shrinkage, snapshot replacement and writable-mode refusal, directory swaps,
and loaded symbol origin. Loader refusal rows also cover unsafe ancestry and FIFO sources.
Separately labeled unit seams cover exact pointer forwarding, descriptor validation and the
second missing-export binder on the default fake.

## Request-feed inventory (producer and admission)

When the loaded codegen module provides `transpile_with_request_inventory`,
the Python bridge calls it once for the strategy in place of `transpile_full`.
Its generated C++, inputs, strategy parameters, diagnostics and input-kind
capability retain their existing behavior. The inventory describes every compiled
bar-request timeframe, including requests for the chart's symbol; it is not a
list of installed symbol-feed files. Older supported codegens without that
function still build ordinary artifacts and carry no inventory.

After linking, the builder calls codegen's `bind_request_feed_inventory` once
with the exact generated UTF-8 and plugin bytes. It verifies both recorded
digests before publishing `request_feed_inventory.json` beside the plugin and
the identical `request_feed_inventory` object in `manifest.json` and
`provenance.json`, inside the artifact directory's atomic publication. Binding
errors fail the build at stage `inventory`. Without a producer, no sidecar or
top-level block is published, including no null placeholder.

`request_identity.request_feed_inventory` records
`"pineforge-request-feed-inventory/v1"` when the loaded producer exists and null
otherwise. This additive key causes existing source caches to rebuild once.
The inventory object stays outside the key, as does the independent `window_plan`
block. Older precompiled artifacts retain ordinary loading and relocation.
Ordinary builds, cache hits and study preparation ignore missing, malformed or
disagreeing inventory blocks and sidecars.

`pineforge_hpo.request_inventory.read_request_feed_inventory` reads the manifest
block and requires an adjacent sidecar, when present, to agree after strict JSON
parsing. It distinguishes missing legacy metadata from an explicit null producer
identity (`producer_unavailable`). A missing block refuses selected execution;
an empty `entries` array is a complete assertion of no request feeds.
`admit_request_feed_inventory` is pure: it validates the exact five-key schema,
source/plugin hashes, optional chart binding and every entry before any trial.
It uses codegen's `canonical_wire_timeframe` without invoking the transpiler.
The Python admission helper therefore needs the codegen inventory module;
ordinary precompiled execution has no such dependency.

Input entries resolve from an effective fixed value or their declared default.
An invalid present value never falls back. For a swept input, every categorical
choice must resolve, independently of other dimensions; numeric, Boolean,
continuous, empty or otherwise non-enumerable domains cannot prove string
timeframe admission. Input aliases such as `1D` resolve to `D`; literal token
entries must already be canonical. Unknown entries and non-null primary bindings
that differ from the study's canonical chart token refuse admission.

Compilation omits `primary_chart_timeframe`, just as the engine's selected Pine
entrypoint does. A request using `timeframe.period` or the chart-timeframe empty
string is consequently `unknown` and is refused by the restrictive policy.
This is a documented selected-mode limit, even when ordinary execution works.

The current Python support policy admits primary chart tokens
`1 3 5 15 30 60 120 240` and request-feed tokens `1 D`.
These mirror the candidate implementation constants in engine
`docker/selected_window_report.py:78-90`, as recorded in spike report-v2 section 2.
The engine's own admission stays authoritative; policy drift can move a refusal
later, and the lane-5 box gate re-proves the constants against the engine.
The Python helper provides admission only; native selected execution and planner
admission have separate contracts. See [failure codes](failure-codes.md) for the
two typed request refusals and their closed arguments.
