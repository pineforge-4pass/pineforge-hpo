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
When getters are absent, HPO synthesizes the engine code using `unknown_key` for
an unsupported setting and `unparseable_value` for other setter refusals; it does
not classify the English diagnostic. A checked factory exception instead carries
`hpo_strategy_create_failed`.

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
the timeout terminal record. An output I/O failure is a process failure, including
when the watchdog must exit 3 after a failed progress write. Warm-start rejection
and exhausted unique space retain exits 4 and 5. Python argparse retains exit 2.

Public Python exception types preserve their standard exception families, messages,
and call signatures. They additionally implement `pineforge_hpo.HpoError` and expose
`.code`, `.origin`, and `.args`. Here `.args` is the failure argument object (or
`None` for unavailable engine arguments), not BaseException's diagnostic tuple;
use `str(error)` for the original message. C++ callers can catch their existing
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
invented empty file. The checked-in target is `version: null, unreleased: true`.

```bash
python3 scripts/gen_catalog_diff.py --check
python3 scripts/gen_catalog_diff.py --release-version 0.10.0
python3 scripts/gen_catalog_diff.py --check --release-version 0.10.0
```

The release workflow stamps the target version and ships both JSON files as
release assets and inside the wheel/source distribution. It verifies that the
wheel's catalog bytes exactly match the assets. Stamping the diff does not modify
the catalog or its raw hash. Regenerate the unreleased diff against the next
previous-release tag before subsequent catalog edits.
