# Stable failure codes

## Trial records

Starting with 0.10.0, every terminal trial object adds three fields immediately after
`error`. The same object is used for `--progress-fd` (including inherited fd 3),
`--trials-file`, `trials[]`, and retained best trials. Existing keys remain unchanged.

```json
{"error":"","failure_code":"strategy_runtime_error","failure_args":{},"failure_origin":"engine"}
```

`failure_code` is a stable code or `null`; `failure_args` is a typed scalar JSON object
or `null`; `failure_origin` is `engine`, `hpo`, or `null`. Successful and merely
constraint-violating trials have all three fields set to `null`. An older plugin may
return an engine error without code getters: its origin is `engine`, but its code and
arguments are `null`, not an invented HPO classification.

Engine codes are forwarded verbatim, including future codes unknown to this HPO
version. Diagnostic text is never parsed to choose a code. Engine arguments are
re-parsed, sorted by key, and dumped without whitespace. Only objects of strings,
finite numbers, integers, booleans, and null are accepted. Invalid JSON, duplicate
keys, nested objects, arrays, or non-finite numbers drop the entire argument object
to `null`. The transport also drops objects exceeding 16 KiB or string scalar values
exceeding 1 KiB. This does not change the engine code or its origin. The `error`
diagnostic is capped at 4096 UTF-8 bytes without cutting a multibyte character.

A run is an `engine_error` when **any** of the following holds: the engine diagnostic
is non-empty, its optional code is non-empty, or its optional last-run status is 1.
In particular, a strategy stopped by `runtime.error("")` cannot contribute a partial
report, train the sampler, or become the best trial. Best results and goldens for
affected studies may change. Unaffected trials retain identical parameters,
metrics, and objectives.

## Optional engine extensions

`strategy_get_last_error_code`, `strategy_get_last_error_args`, and
`strategy_last_run_status` are optional dynamic symbols. Missing extensions do not
prevent loading an older plugin. Exact `pf_abi_version` matching is still required;
this extension does not relax the ABI check.

The adapter prefers `strategy_create_checked`, `strategy_set_input_checked`, and
`strategy_set_override_checked` when each symbol is available. An absent symbol
uses its legacy counterpart. Checked failures throw rather than silently dropping
an unknown title, override key, invalid enum, or unparseable value. Setter refusals
carry engine-owned `setting_rejected` metadata, using the engine's closed
`entrypoint` and `reason` vocabularies. When the engine supplies a more specific
code/argument object, HPO forwards it unchanged apart from argument normalization.
When no getter code is available, an invalid-argument setter status uses
`setting_rejected` with only `entrypoint`: the status cannot establish a reason,
so HPO omits `reason` rather than classifying the English diagnostic. An unsupported
status uses `setting_unsupported` with no arguments; other failed setter statuses
use `engine_unclassified_error` with no arguments. Every non-OK checked factory
status instead carries `hpo_strategy_create_failed`.

An ambiguous input title is one status-only `UNSUPPORTED` case: the native runner
reports `setting_unsupported` with `{}`, while the engine harness reports
`setting_rejected` with `reason: ambiguous_key`. The native runner deliberately
does not infer that reason from English text. Once the engine supplies latched
setter metadata, the forwarding branch preserves its specific code and arguments.

Python's existing manifest/override preflight also uses `setting_rejected` for
known setting refusals. The optional `input` argument is only the codegen manifest's
literal Pine input title. HPO-specific domain, study, and artifact consistency
validation remains an HPO-owned failure.

## Initialization and process failures

The native and Python CLIs print one stdout document for an initialization or
process failure, while preserving existing exit codes and stderr diagnostics:

```json
{"schema_version":1,"ok":false,"failure":{"origin":"hpo","code":"hpo_plugin_invalid","args":{"reason":"load"},"exit_code":1}}
```

Trial failures still appear in ordinary study results. Exit 2 still denotes no
feasible best trial; ordinary watchdog timeout results still use exit 3 and include
the timeout terminal record. A progress-I/O failure still publishes completed trials
to stdout and `--output` when writable, adding a top-level `failure` object with
`origin`, `code`, `args`, and `exit_code` to the ordinary result document. It exits 1;
the watchdog preserves the timeout terminal record and exits 3. The result's `ok`
field continues to describe whether a feasible best trial exists; inspect `failure`
and the process exit code to detect a publication failure. Warm-start rejection
and exhausted unique space retain exits 4 and 5. Python argparse retains exit 2.

Public Python exception types preserve their standard exception families, messages,
and call signatures. They additionally inherit the catchable exception base
`pineforge_hpo.HpoError` and expose `.code`, `.origin`, and `.failure_args`. The latter
is the failure argument object (or `None` for unavailable engine arguments).
`BaseException.args` remains the writable diagnostic tuple; `str(error)` retains
the original message. C++ callers can catch their existing
standard exception family or inspect the `pineforge::hpo::HpoError` metadata mixin
declared in `include/pineforge/hpo/error.hpp`.

The native CLI reports `hpo_seccomp_unavailable`, class `environment`,
`retryable: false`, when the requested seccomp layer cannot be installed. Its one
closed string argument, `reason`, is `architecture`, `filter`, `headers`, `kernel`,
`no_new_privs` or `platform`. It exits 1 before plugin loading and publishes no
result file. This code appears only when `--seccomp` was requested; a run without
the flag is never refused by it.

The Python planner-identity API raises `WindowPlanError` (also `HpoError` and
`RuntimeError`) and the native planner loader throws a typed runtime error, both
with `hpo_walkforward_unavailable`, class `environment`, `retryable: false`, and
one closed string argument, `reason`. The Python identity checks use these reasons:

| Reason | Refusal |
| --- | --- |
| `window_plan_path` | The explicit helper path is relative or the selected archive has no derivable companion and no explicit absolute path was supplied. |
| `window_plan_missing` | The resolved helper file is absent, unreadable or not a regular file. |
| `window_plan_manifest_missing` | Selected precompiled admission has no `manifest.window_plan`; a path alone cannot supply its expected identity. |
| `window_plan_manifest_invalid` | The block has the wrong shape, types, schema, hashes, ABI or self-test ID. |
| `window_plan_identity` | Manifest/provenance disagree, their pair is incomplete, or the helper's engine/platform association differs from the recorded compile identity or local engine. |
| `window_plan_hash_mismatch` | The helper file's SHA-256 differs from the recorded expected hash. |

The native loader (`WindowPlanHelper::load`, see `docs/native-walkforward.md`) adds
these reasons. It also reports `window_plan_path` (a relative or NUL-containing helper
path) and `window_plan_hash_mismatch` (a malformed expected hash, or a snapshot whose
bytes differ from it), but never `window_plan_missing`: an absent or unreadable source
is `window_plan_load`.

| Reason | Refusal |
| --- | --- |
| `window_plan_changed` | The source or the private snapshot shrank or changed while it was copied, verified or loaded; a retry against a stable source can succeed (the catalog's `retryable` flag is false for the code as a whole). |
| `window_plan_load` | The source is absent, unreadable or not a regular file, the snapshot could not be created, `dlopen` refused it (for example a `noexec` run directory), the platform has no POSIX loader, or the helper returned an invalid descriptor, option or bridge status. The same reason covers a run directory that is not an absolute, resolvable, private, user-owned directory with safe ancestry, a request string containing NUL, a helper object with no bound bridge (for example moved from), and an unexpected exception while loading. |
| `window_plan_missing_pf_selected_plan_version` | The loaded library lacks the `pf_selected_plan_version` export. |
| `window_plan_missing_pf_plan_selected_primary_v1` | The loaded library lacks the `pf_plan_selected_primary_v1` export. |
| `window_plan_version` | The helper does not report bridge version 1. |
| `window_plan_self_test` | The load-time golden self-test (`selected-plan-utc-six-hours/v1`) failed. |

The selected-window plugin binding (`StrategyPlugin::require_selected_window` and
`SelectedEvaluation`, see `docs/native-walkforward.md`) adds these reasons; the setter
statuses -2 map to the two `*_contract` reasons, while -4 is the engine's `out_of_memory`
and every other refusal is `hpo_invariant`:

| Reason | Refusal |
| --- | --- |
| `missing_pf_selected_window_version`, `missing_strategy_set_selected_window_v1`, `missing_strategy_selected_window_counts_v1`, `missing_pf_execution_observer_version`, `missing_strategy_set_execution_observer_v1`, `missing_strategy_execution_observation_v1` | The plugin lacks this one of the six selected-window / observer exports; the reason names the first absent one in protocol order and the diagnostic lists them all. |
| `selected_window_version`, `execution_observer_version` | The plugin's selected-window or execution-observer ABI version is not 1. |
| `selected_window_contract`, `execution_observer_contract` | The plugin refused the selected-window or observer setter with status -2 (unsupported contract). |
| `engine_version_missing` | The plugin lacks `pf_version_get` or `pf_version_string`, or the string is null. |
| `engine_version_invalid` | The embedded version string is malformed (grammar `X.Y.Z[-rc.N][-D-gSHA][-dirty]`) or disagrees numerically with `pf_version_get`. |
| `engine_revision` | The embedded engine is not a stable 1.x release at least 1.6.2 (any release candidate is refused, dirty or not). |
| `engine_identity` | The full embedded version string differs from the expected manifest version. |

The Python checks do not load the helper. Diagnostics name affected fields or files;
paths and diagnostic text are not failure arguments. An existing expected hash
is never rewritten to accept different bytes. Metadata publication I/O retains
`hpo_output_io_failed`; an interrupted two-file enrichment leaves selected
admission refused until enrichment is re-run. Ordinary execution ignores the
`window_plan` block and does not require a planner companion.

`plan_selected_window` throws `TypedHpoError<std::invalid_argument>` with
`hpo_walkforward_plan_refused`, class `request`, `retryable: false`, since `1.8.0`.
Its template is `Selected-window plan refused ({status}: {option}).`; both arguments
are required closed strings. The `option` values are `calendar`, `end_ms`,
`engine_timezone`, `fed_start_ms`, `input_tf`, `none`, `preroll_bars`, `rows`,
`script_tf`, `session`, `start_ms`, `timeframes`, and `timezone`. `none` represents
an empty engine option.

| Status | Planner refusal |
| --- | --- |
| `boundary_unaligned` | T, E or F is not a nominal script open; boundaries are never snapped. |
| `calendar_unsupported` | The clock, session, native calendar or timeframe pairing cannot serve the request. |
| `feed_range_invalid` | A supplied row is below F or at/after E; the bounded DatasetView route excludes these rows before calling the planner. |
| `internal_error` | The engine planner reports an internal exception or disagreement. |
| `request_invalid` | A request member or row descriptor is invalid. |
| `rows_unordered` | Supplied timestamps are not strictly increasing; Dataset construction already refuses such tapes. |

Diagnostics include `value_ms`, numeric `bound` and its T/E/F word (or `none`),
`previous_boundary_ms` and `next_boundary_ms`, plus affected request strings where
applicable. Those values are diagnostic text, never extra catalog arguments.
An undocumented refusal option and inconsistent successful plans are
`hpo_invariant`; the raw-result test seam also guards unknown statuses.
Loader and C bridge failures, including malformed status words, propagate as
`hpo_walkforward_unavailable`, including reason `window_plan_load`. A valid
pre-roll shortfall is returned for caller policy.

### Request-feed inventory admission

Since 1.8.0 the selected-study Python API raises `RequestTimeframeError` or
`RequestInventoryError` (both `HpoError` and `RuntimeError`). Both codes are class
`request`, `retryable: false`. They translate the engine's
`window_mode_unsupported` admission vocabulary into closed HPO arguments:

| Code | Arguments | Template |
| --- | --- | --- |
| `hpo_walkforward_timeframe_unsupported` | `capability`: `selected_window_chart_timeframe` or `selected_window_request_feed_timeframe` | Unsupported selected-window timeframe ({capability}). |
| `hpo_walkforward_inventory_invalid` | `capability`: `selected_window_request_feed_inventory`; `reason`: the closed list below | Invalid request-feed inventory ({capability}: {reason}). |

| Inventory reason | Refusal |
| --- | --- |
| `missing` | The manifest or inventory block is absent, including an older artifact whose request identity has no inventory-producer key. A sidecar alone cannot supply the block. |
| `invalid` | JSON has duplicate keys or is malformed, schema/shape/token/hash spelling is invalid, a present block is null, or a present sidecar disagrees with the manifest block. |
| `unbound` | `artifact_sha256` is null. |
| `artifact_mismatch` | Source or plugin digest differs from the supplied generated-C++ or linked-plugin identity. |
| `chart_mismatch` | A non-null primary binding differs from the study's canonical chart token. |
| `unknown` | At least one entry is unknown, including a chart-timeframe request compiled without a chart binding. |
| `input_unresolved` | A present fixed value or the effective default is not a string timeframe token, or a referenced swept domain cannot prove that every reachable value is a token. |
| `producer_unavailable` | The inventory block is absent and `request_identity.request_feed_inventory` explicitly records null (that build had no producer), or admission cannot import `pineforge_codegen.request_feed_inventory`. |

Empty `entries` is a complete declaration and is admitted. The reader compares
an optional adjacent sidecar after strict JSON parsing; the manifest block remains
required. Offending tokens, input keys and file paths appear in bounded diagnostic
text, never as extra catalog arguments. Binding failures during compilation use
`ArtifactBuildError(stage="inventory")` with the existing
`hpo_toolchain_unavailable{reason:transpile_internal}` classification.
Ordinary artifact loading and cache hits ignore inventory metadata.

## Catalog and release diff

The package contains `hpo_failure_codes.json` (schema
`pineforge-hpo-failure-catalog/v1`) and `hpo_failure_codes_diff.json` (schema
`pineforge-hpo-failure-catalog-diff/v1`). Each HPO code declares its class,
retryability, typed arguments, closed string vocabularies, English templates, and
first release. Names match `^[a-z][a-z0-9_]{2,47}$`; a code is never reused for a
different meaning. Engine-owned codes are intentionally not duplicated in this
catalog.

The diff names the previous release and raw catalog SHA-256, added entries, changed
field paths with before/after presence and values (including vocabulary changes),
and deprecations with `replacedBy`. Removing an existing code is rejected. The
0.9.0 baseline did not contain a catalog, so its hash is `null`, not a hash of an
invented empty file. The 0.10.0 release commit carries target `version: "0.10.0"`
with `unreleased` absent. Development diffs instead use `version: null, unreleased: true`.

```bash
python3 scripts/gen_catalog_diff.py --check
python3 scripts/gen_catalog_diff.py --release-version 0.10.0
python3 scripts/gen_catalog_diff.py --check --release-version 0.10.0
```

The release workflow stamps the target version and ships both JSON files as
release assets and inside the wheel/source distribution. It verifies that the
wheel's catalog bytes exactly match the assets. Stamping the diff does not modify
the catalog or its raw hash. Repeating the release stamp on the already-stamped
release tree is byte-identical, including after its tag is created. Generation and
`--check` use the explicit baseline
pinned in the diff's `from.tag`, not the newest merged tag, so checks remain valid
after a release tag is created. Before the next release cycle's catalog edits,
advance that baseline explicitly with `--from-tag vX.Y.Z`; an initial diff also
requires `--from-tag`. Release stamping refuses an outdated baseline: `from.tag`
must equal the newest merged release tag strictly older than the target version.
For example, after `v0.10.0` exists, stamping `0.11.0` with baseline `v0.9.0`
fails and asks for `--from-tag v0.10.0`. Generating a new unreleased diff also
requires the newest merged release baseline; after that tag, run
`python3 scripts/gen_catalog_diff.py --from-tag v0.10.0`. Its `from` catalog hash
then matches the 0.10.0 release catalog. Plain unreleased checks keep using their
pinned baseline and remain valid after tagging.

## Regression proof

CI builds the 0.9.0 native runner and compares a valid three-candidate grid study
against the current runner. Existing trial fields, including parameters, metrics,
and objective, must have identical canonical bytes. The same generated strategy
with an empty `runtime.error()` must reproduce the old partial-report success and
be rejected by the current runner. The test also exercises checked input and
override refusals against the real engine. Getter presence is asserted explicitly;
the pinned older engine expects null failure metadata for the empty runtime error.

```bash
python3 tests/test_failure_codes_e2e.py \
  --native build/bin/pineforge-hpo-native \
  --baseline build/failure-baseline/bin/pineforge-hpo-native \
  --engine-root external/pineforge-engine --engine-codes absent \
  --output build/failure-e2e
```

Use `--engine-codes present` when validating an engine with the optional getters.
The output directory retains before/after observations, failure records, and hashes.
