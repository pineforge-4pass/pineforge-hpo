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
ACLs and a group-only owner are not covered by this ancestry check.

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

## Selected-window planning over a DatasetView

Include `pineforge/hpo/selected_planning.hpp` and call
`plan_selected_window(helper, tape, request)` with an already loaded `WindowPlanHelper`,
an immutable `DatasetView`, and one `PlanRequest`. The returned `SelectedPlan` owns the complete
`PlanResult`, a retained `DatasetView`, the six `SelectedExpectedCounts`, and
`supplied_first_index` (the owner-relative first supplied row, before trimming).
Supply `retained.data()`, `retained.size()` and `expected` to `SelectedEvaluation::evaluate`
with the same T/E and strategy clock/settings.

The tape may span later folds or be a prefix or subview of its owner. Two binary searches
cut the supplied rows to **F <= timestamp < E**. Rows before F and at/after E are excluded
before calling the engine, which refuses out-of-range rows rather than truncating them.
The request, including T/E/F/N, timeframes, clock, session and feed-tolerant flag, reaches
the helper unchanged; no boundary is snapped. An empty cut is passed to the planner, including
a possibly null pointer with zero count. An invalid F/E relationship also reaches request
admission without unsigned range underflow.

Binary search relies on Dataset's existing strict timestamp ordering: the
`Dataset(std::vector<pf_bar_t>)` constructor checks it, CSV loading uses that constructor, and
mapped loading calls `check_binary_records`. Preserve the owner's immutability, including mapped
bytes; mapped-file verification remains the owner's run-boundary duty. Valid Dataset inputs cannot
reach `rows_unordered`, and the bounded cut prevents `feed_range_invalid`. Their refusal mappings
remain defined for every engine status.

Planner refusals throw `TypedHpoError<std::invalid_argument>` with
`hpo_walkforward_plan_refused` and exactly two closed string arguments, `status` and `option`.
An empty engine option becomes `none`. Diagnostics preserve the offending value, bound and
T/E/F word, previous/next boundaries, and relevant request strings. Helper load/bridge errors
propagate unchanged. A successful result with inconsistent counts, masks, timestamp ranges
or row endpoints throws `hpo_invariant` naming the field.

Validation checks supplied count against the bounded view, the retained-tail equality, the
input and script partitions, supplied script groups, used/trimmed pre-roll groups, shortfall,
both Boolean words and optional timestamp presence/order/ranges. It also independently
searches the first row at/after T and checks the exact supplied/fed/window row endpoints.
The helper already admits descriptor size/version/reserved, status/bound/mask encoding,
option encoding and zero absent timestamps. Planning repeats Boolean and absent-timestamp
checks for its separately testable raw-result seam; it adds the semantic checks above.

Pre-roll optional timestamps are **script open labels**, so the first can precede the first
retained input row. Supplied, fed and window optional timestamps are **input row timestamps**.
Window timestamp presence follows `window_input_bars`, even when the last window group is
unsealed and `window_script_bars` is zero. No equality between script and input counts is
assumed.

| SelectedExpectedCounts field | PlanResult::raw source |
| --- | --- |
| `fed_input_bars` | `fed_input_bars` |
| `fed_script_bars` | `fed_script_bars` |
| `preroll_input_bars` | `preroll_input_bars` |
| `preroll_script_bars` | `used_script_bars` |
| `window_input_bars` | `window_input_bars` |
| `window_script_bars` | `window_script_bars` |

`retained` is exactly the tail beginning at `trim_index` of the bounded cut, with
`fed_input_bars == supplied_input_bars - trim_index`. Its pointer is the bounded pointer plus
that offset (preserving a possibly null empty-owner pointer at offset zero). Bars are never
copied or repacked, and the view retains the original owner. Index additions are bounded by
DatasetView's constructor checks, and count identities use guarded subtraction.
Cutting and endpoint checking take O(log n) time with constant auxiliary storage; the engine
planner still walks the bounded rows. Calls are thread-compatible with immutable input and a
helper whose lifetime spans every call.

The six execution expectations omit `trimmed_script_bars`, `available_script_bars`,
`shortfall` and `complete_pending_preroll_at_horizon`; these remain in `plan.raw` for logging
and caller policy. **A pre-roll shortfall is returned, not thrown.** An empty plan is also
valid planner output; `SelectedEvaluation` separately requires nonempty executable input.
This library function plans one request; inventory admission, CLI/StudySpec forwarding,
objective scoring and the loop over windows belong to later integration.

The `pineforge_hpo_selected_planning` test uses the real helper with the same
`PFH_WINDOW_PLAN_LIB` / `PFH_REQUIRE_WINDOW_PLAN=1` convention and private run directories
as `pineforge_hpo_window_plan`. Missing helpers report `UNAVAILABLE` and fail in required
mode. Hand-derived five-minute fixtures cover two folds, bounded-view equivalence, exact
pointer offsets and owner lifetime, empty input, zero pre-roll, shortfall, pending pre-roll
and an unsealed window tail. Direct real-helper arrays establish out-of-range and unordered
refusals, with conversion through a labeled internal seam. Separate hand-built results
exercise validation failures and internal-error vocabulary. These are predictions; real-plugin
execution and native count/trade parity remain separate gates.

## Selected-window plugin binding and OOS evaluation

`StrategyPlugin` optionally binds the engine's six selected-window and execution-observer
exports. Ordinary execution requires none of them. Selected execution refuses the first missing
export in protocol order and lists all missing exports in its diagnostic, then checks both wire
versions are 1. It next reads `pf_version_get` and `pf_version_string` from the loaded plugin:
numeric components must agree, the runtime must be stable engine 1.x at least 1.6.2, and any
supplied expected manifest version must exactly match the full embedded version. Release
candidates are refused, including dirty release candidates. Git-describe versions use
`X.Y.Z[-rc.N][-D-gSHA][-dirty]`: dirty trees are admitted with or without a distance/SHA suffix,
including an exactly tagged dirty tree such as `1.7.0-dirty`. The exact full string is the
attribution recorded in the manifest and compared by `engine_identity`. This release gate does
not attest a custom build's provenance or replace real-plugin parity evidence.

The six exports, in admission order, are `pf_selected_window_version`,
`strategy_set_selected_window_v1`, `strategy_selected_window_counts_v1`,
`pf_execution_observer_version`, `strategy_set_execution_observer_v1`, and
`strategy_execution_observation_v1`. Missing exports, incompatible wire versions, contract
refusals and engine-identity refusals use `hpo_walkforward_unavailable` with distinct reasons.
Setter allocation refusal (-4) is engine `out_of_memory`. Other setter refusals, getter
refusals, mutated headers and post-run correlation failures use `hpo_invariant`; a setter's -2
uses `selected_window_contract` or `execution_observer_contract`. Counts -2 after a successful
run never authorizes scoring.

`SelectedEvaluation::evaluate` consumes already-planned `[T,E)` bounds, an immutable retained
row pointer/count, and six expected counts. It neither loads nor calls the planner. The expected
`preroll_script_bars` is the planner's **used** script groups. Each call admits the plugin,
creates one fresh handle and applies the same timezone, overrides, inputs, symbol metadata and
feed setter sequence as an ordinary trial. It optionally reads version-1 effective-settings JSON
from that handle using BUFFER_TOO_SMALL then OK, with a 16 MiB bound. An absent or unusable
receipt yields `effective_settings == nullopt`; consumers must treat that as **UNATTESTED**,
never as a settings attestation.

The helper configures selection, installs a native observer and invokes `run_backtest_full`
once on the retained pointer. Fresh handles keep the engine's full owning report mode; this
helper never requests summary-only mode, which the observer refuses. The callback catches all
exceptions, records exactly one boundary and replaces the receipt's UINT32_MAX sentinel with
the valid non-exporting 0/0/0/0 receipt. After return, a retained callback failure wins over engine
errors. The helper then checks the engine error/code/status, the one successful callback,
three nonzero count/observation identities, six plan/count equalities, Results/ResultsOpen state,
boundary delivery and absence of faults, callback identity, processed **fed** counts and
`window_script_bars + 1` equity points. Only then does shared report capture create a snapshot.
The objective layer can score only a returned snapshot.

The engine harness also checks `report.input_tf_seconds` against the request; this helper has no
equivalent check. Timeframe and feed admission belong to the planner lanes (4a/4b).

When `pbo_blocks` is requested, the block layout and moments depend on the selected window's
curve length. Blocks are comparable only across trials of the **same window**.

Cleanup removes the observer while its context is alive, clears pending selection, then frees
the report before the handle. If removal fails, the context survives handle destruction.
Cleanup cannot replace an earlier failure, and a cleanup refusal cannot return a successful
result. Returned values include the native counts, observation, boundary and optional settings
receipt alongside the report.

At most **one selected evaluation may be in flight per worker**. The helper copies the report,
including trades and equity even if ordinary capture flags are disabled, and immediately releases
the engine report. The detached report costs O(window bars + trades); the caller must stream its
window row, trade rows and equity segment to its sink and release the value before evaluating the
next window. The helper retains no window history.

Unit fixtures are explicitly opted in and separately named. The default fake has none of the six
exports, enforced by source inspection and post-build/CTest defined-symbol guards on Linux and
Darwin. These tests do not establish real-engine selected behavior. Real current-plugin parity,
an older missing-export archive and a parity-era revision refusal require the separate real-plugin
gate. The adapter and these native tests require the full dispatched CI build on Ubuntu and
macOS or a box; PR core CI does not compile this path.

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
Callers must pass the verified SHA-256 of the plugin file that will be loaded and
of its generated C++ (manifest values verified against the files by the artifact
loader). Admission performs no I/O and compares the inventory's hashes only with
the hashes it is given, so its identity check is only as good as those arguments.
It uses codegen's `canonical_wire_timeframe` without invoking the transpiler.
The Python admission helper therefore needs the codegen inventory module;
an unavailable module refuses with `producer_unavailable`. Ordinary precompiled
execution has no such dependency.

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
