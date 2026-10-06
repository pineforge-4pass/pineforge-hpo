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
