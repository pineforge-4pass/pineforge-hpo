# Native walk-forward

## Planner helper loader

`WindowPlanHelper::load(absolute_path, expected_sha256_hex, private_run_directory)` loads the
standalone `libpineforge_window_plan` companion. The caller supplies its trusted absolute source
path, the expected 64-character lowercase SHA-256, and an existing absolute run directory owned
by the effective user with no group or other permissions. Relative source paths are refused
before normalization or filesystem access. There is no implicit path search or temporary-directory
fallback. A worker owns one helper and reuses it for its planning calls.

The loader opens the source read-only with close-on-exec, requires a regular file, and copies
from that held descriptor into a new randomly named, exclusive snapshot created with mode 0600.
It streams SHA-256 over the copied bytes and checks the source descriptor's device, inode, size,
and full mtime/ctime before and after copying. It refuses changed sources with
`hpo_walkforward_unavailable{reason:window_plan_changed}` and unequal content with
`reason:window_plan_hash_mismatch`, before loading any helper code. It then makes the snapshot
0400, closes its write descriptor, checks the snapshot path identity, and calls `dlopen` only on
the absolute snapshot path with `RTLD_NOW | RTLD_LOCAL`. Source symlinks are followed once by
`open`; a later source path or symlink replacement cannot redirect the snapshot copy.
The helper retains the opened source's device, inode and byte length, its verified SHA-256,
and the actual snapshot path for runtime provenance.

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
for the worker lifetime, then owns cleanup of successful and failed-load snapshots. Snapshot
storage is one helper copy per worker, independent of the number of planner calls. The loader
does not execute a strategy, admit request-feed inventories, or implement selected execution.

The `pineforge_hpo_window_plan` CTest test reads the real engine helper from
`PFH_WINDOW_PLAN_LIB`. It reports `UNAVAILABLE` when the helper is unset or missing, and that
condition fails when `PFH_REQUIRE_WINDOW_PLAN=1`. Non-PR native CI builds the engine target
`pineforge_window_plan`, requires its exact `build/lib/libpineforge_window_plan.so` (Linux) or
`.dylib` (macOS) output, and enables the required mode for CTest. PR core CI does not compile
the adapter. Real helper vectors cover the golden fixture, request and boundary refusals,
unordered direct arrays, feed range, clock/pairing, pending pre-roll completion, source swaps,
in-place mutation and loaded symbol origin. Separately labeled unit seams cover exact pointer
forwarding, descriptor validation and the second missing-export binder on the default fake.
