# Sobol native descriptor and parent admission (internal note)

Status: source preparation, UNEXECUTED. Nothing described here was compiled or run when it was
written. The first compile and every test result belong to the spot proof. This note is for the one
integrator who wires the Sobol sampler into the native runner; it changes no public behavior by itself.

## Scope

The helper lives in `src/cli/sobol_continuation.hpp` (header only, namespace `pineforge::hpo::detail`).
It owns three things from the Sobol contract (N9, N10, N12):

1. the descriptor of a Sobol run and its identity hash;
2. constant-memory accounting of one part's terminal trial IDs (first index, next index, exactness);
3. admission of a complete Sobol result as the parent of a continuation.

It does not touch the runner, CMake, Python, notices, the version, the engine, the X helper or the
existing `continuation.hpp`. It adds no checkpoint, REL31 or other persistent format: the descriptor is
a block inside the result JSON.

## Descriptor and identity

`make_sobol_descriptor(sampler, space_hash, numeric_build_identity)` returns a `SobolDescriptor`:

| Field | Source |
|---|---|
| contract | `pineforge_sobol_v1` |
| implementation | `pineforge_sobol_gray64_joe_kuo_d6_v1` |
| table name, subset hash, upstream hash | the engine's `kSobolTable*` constants |
| word bits | 64 |
| scramble | `none` or `digital_shift` |
| seed | the run seed as a decimal string for `digital_shift`, null for `none` |
| columns | varying dimension names in column order (byte-wise UTF-8 order) |
| space hash | the result's `space_hash` (already covers parameters and objective) |
| numeric build identity | supplied by the integrator; null exactly when no column uses binary64 math |
| mapper contract and revision | `pineforge_sobol_mapper_v1` and `kSobolMapperRevision` (2) |

The identity is the SHA-256 of the canonical JSON of {columns, contract, implementation,
mapper_contract, mapper_revision, numeric_build_identity, scramble, seed, space_hash,
table_subset_sha256, word_bits}. Keys are sorted, there is no whitespace, 64-bit values are decimal
strings, and `word_bits` and `mapper_revision` are JSON numbers. `sobol_identity_input()` returns that
text so any other implementation can recompute the hash. Two additions to the literal N10 list are
deliberate and are the one item AR may veto: `mapper_contract` and `mapper_revision` (the amendment asks
that the revision be bound in the Sobol identity) and the `mapper` object in the block. The block has
exactly twelve keys: contract, table, word_bits, scramble, seed, columns, numeric_build_identity,
mapper, identity, first_index, next_index, exact_stream.

The numeric build identity is a verified component supplied by the final integrator. This helper never
derives it, never reads the TPE flags hash and never claims an actual-flags proof. It throws an
invariant error when the identity is missing for a space that needs it, or present for a discrete-only
space, and admission refuses a string containing `flags_sha256:unavailable`.

## Part accounting

`SobolPartAccumulator` takes every terminal trial ID of the part (any status) at the point where the
archive sees it, whether or not the row is retained, so best-k, none, cancelled, timed-out and fatal
results are described correctly. `summary(first_index, parent_exact)` returns:

- `next_index`: the largest terminal ID plus one, or `first_index` when the part has no terminal trial;
- `exact_stream`: `parent_exact` and the part's IDs are exactly `first_index` to `next_index - 1`.
  An empty part is vacuously exact, so it inherits the parent's exactness.

IDs below `first_index`, repeated IDs and the reserved ID 2^64-1 are invariant errors. Holes are kept:
the next run always starts at the largest ID plus one and nothing below it is reissued.

## Parent admission

`admit_sobol_parent(path, space, recorded_space, expected_descriptor, max_trials)` returns a
`SobolAdmission` holding the generic loader's validated `WarmHistory` (for `Options::warm_history`),
`next_id` (the child's first index), `parent_exact`, and the ancestor and part row counts. Every
refusal is the existing `WarmStartError` (`hpo_warm_start_rejected`, exit 4). Messages are the fixed
`sobol_refusal` constants, optionally followed by a static field name; none contains a path or a value
from the parent. Messages that come from the generic loader are that loader's own.

Order, cheap before heavy:

1. Format: a JSON object with a `trials` array. Rows-only arrays, JSONL, binary history and anything
   unparseable are refused. `sampler` must be `sobol`, `trials_out` must be `all`.
2. Descriptor: exact-key `sobol` block; every field compared with the run's descriptor; top-level
   `sampler_implementation`, `space_hash`, `space_hash_version` and (for `digital_shift`) the numeric
   `seed`; the parent's identity hash is recomputed from the parent's own fields and must match both its
   claim and the run's identity.
3. Rows: `trials_completed` and `warm_start.trials` must equal the array lengths; there must be at
   least one row; trial IDs are parsed with the generic `json_id` (which refuses 2^64-1) and must be
   unique; `first_index` must be 0 without ancestors and the largest ancestor ID plus one otherwise; no
   part ID may be below it; `next_index` must be `first_index` for an empty part and the largest part ID
   plus one otherwise; `exact_stream` is recomputed from the rows and any contradiction is refused.
4. The generic loader (`load_json_warm_history`) runs unchanged: space and objective identity, symbol
   feeds, statuses, parameter validity and canonicalization, and the same 256 MiB caps.
5. The loader re-reads the file, so its content digest must equal the digest taken in step 1.
6. Every row, ancestors included, must equal `SobolSampler::at(trial_id)` under the descriptor, bit for
   bit for reals. A forged row is refused; metadata can never make it exact.
7. Budget: `next_id` must not be 2^64-1 and `max_trials` must not exceed 2^64-1 minus `next_id`.

Gapped, cancelled and timed-out parents are admitted with `parent_exact` false. A parent whose combined
rows are exactly IDs 0 to `next_id - 1` yields `parent_exact` true, and the child's parameter stream is
then the shifted parameter stream of an uninterrupted run. Nothing is claimed about whole results.

A Sobol result with no row at all (empty part and no ancestors) cannot be a parent; it is refused with
an explicit message, as the generic loader does. Re-run it fresh; the stream is the same.

## Call sites for the integrator (nothing here was edited in the runner)

Line numbers refer to the baseline `main.cpp` of commit `d2f83326`.

1. After `options.space_hash` is set (near line 1896), for `sobol` only:
   build `SobolSampler(space, options.seed, scramble, 0, options.max_trials)` to surface refusals before
   billing, require the portable environment when `sampler.uses_floating_point()`, obtain the verified
   numeric identity (null otherwise), and call `make_sobol_descriptor(sampler, options.space_hash, numeric)`.
2. At the continuation block (lines 1897 to 1912), for `sobol` call `admit_sobol_parent(options.warm_start,
   space, recorded, descriptor, options.max_trials)` instead of `load_warm_history`, store
   `admission.history` as `options.warm_history`, and keep `admission.parent_exact`. Skip the
   `SpaceExhausted` test, the finite-policy block and the ID-overflow test (admission did the last one):
   Sobol is with-replacement and `candidate_policy` must be `sampler_default`.
3. At the sampler branch (line 2128): `SobolSampler(space, seed, scramble, first_index, options.max_trials)`
   with `first_index = admission.next_id` or 0; take candidates from `next()`; the ID is the index; do not
   call `warm_history->contains()` and do not reseed with `continuation_seed()`.
4. In `TrialArchive::add` (line 897): `part.observe(record.trial_id)` for every terminal record, before the
   `trials_out` retention decision.
5. In `render_results` (line 1384): `sampler_implementation` for `sobol` (the current lambda would
   report the grid name); one more key `"sobol": render_sobol_block(descriptor, part.summary(first_index,
   parent_exact))`; `unique_candidates_attempted` and the other finite-space statistics may be reported
   but must not produce the `search_space_exhausted` stop reason for this sampler.
6. In the timeout and failure paths (lines 1198 and 1986) the same function renders the block from the
   accumulator, so partial and fatal results carry correct metadata.

## Minimal shared changes (listed, not made)

- Options fields for the scramble, the descriptor, the accumulator and `parent_exact`; the usage text and
  the `--sobol-scramble` flag (spellings from `sobol_scramble_from_name`).
- CMake: register `tests/test_sobol_continuation.cpp` with include directories `src/cli`, `src/core`,
  `include`, the definition `PFH_SOBOL_CONTINUATION_FIXTURES`, and link `PineForgeHPO::core`.
- Optional, performance only: a loader entry point that accepts an already parsed document and its
  content digest would avoid parsing a large parent twice. Correctness does not need it: the digest guard
  in step 5 makes the double read safe.
- Doxygen: `Doxyfile` recurses over `docs` with warnings as errors, so this note is part of that gate;
  the owner may prefer to add `*/docs/internal/*` to `EXCLUDE_PATTERNS`.

## Unresolved needs

- The verified Sobol numeric build identity (source and flag binding) is the final integrator's input.
- A parent containing subnormal real parameters depends on how `Json::real()` treats `ERANGE` from
  `std::stod`; that is the generic loader's behavior and outside this helper.
- `kChanged` (the file changed between the two reads) is reachable only with a concurrent writer and is
  not exercised by the unit tests.

## Proof commands for the spot box

```
cmake -S . -B build -DPINEFORGE_HPO_BUILD_TESTS=ON
cmake --build build --target pineforge_hpo_test_sobol_continuation pineforge_hpo_test_sobol_mapper
ctest --test-dir build -R sobol --output-on-failure
```

The first two lines assume the integrator registered the targets named after the test files. The mapper
test needs `float-reference.tsv` from `python3 -I mapper_reference.py` (see the mapper fixtures README).

## Not claimed

No runtime result, performance figure, cross-architecture equality or quality claim. Exactness is the
shifted parameter stream only, never equality of whole result objects.
