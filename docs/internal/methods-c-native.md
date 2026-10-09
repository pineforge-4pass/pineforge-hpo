# C native input/list leaf: `src/cli/candidate_list.hpp`

Status: leaf code and native tests only, written 2026-10-09 against base `d2f83326`. Nothing is
wired into the CLI. The header was checked with `clang++ -std=c++17 -fsyntax-only -Wall -Wextra
-Wpedantic` (no object produced, nothing run). The tests in `tests/test_candidate_list.cpp` have
**not been executed**; the first build and run is the spot phase. Binding semantics are the AR
pin `methods-c-contract.pin.md`; the proposal is support only. This note is internal: it is not
product documentation and promises nothing about billing, whole-process memory or warm import.

## What the leaf owns

| Piece | Purpose |
|---|---|
| `read_candidate_list_file`, `parse_candidate_list`, `load_candidate_list` | bounded regular-file JSONL admission of the whole list before any plugin or dataset is touched |
| `CandidateList` | immutable canonical vectors in list order, `source_sha256`, `list_sha256`, `at(position)` |
| `CandidateListCursor` | ordered proposer for `evaluate_batches`; duplicates are separate occurrences |
| `CandidateCoverage`, `candidate_list_json` | terminal-position bitmap for the archive's lock, plus the `candidate_list` block |
| `require_candidate_list_settings`, `require_candidate_list_budget` | the pin's flag and budget refusals, as typed errors |

Everything is in `pineforge::hpo::detail`, header-only, C++17, POSIX (`open`/`fstat`/`read`, as
`main.cpp` already uses). It includes `continuation.hpp` and reuses its `scalar_value`,
`Json::real`, `candidate_key`, `WarmStartError` and the `SearchSpace` calls the importer makes.

## API

```cpp
inline constexpr std::string_view candidate_list_format = "pineforge_candidates_v1";
inline constexpr std::string_view candidate_list_implementation = "pineforge_candidate_list_v1";
inline constexpr std::uint64_t candidate_list_max_occurrences = 50'000;
inline constexpr std::uint64_t candidate_list_max_file_bytes = 32ULL * 1024 * 1024;
inline constexpr std::uint64_t candidate_list_max_line_bytes = 64ULL * 1024;
struct CandidateListLimits { max_occurrences, max_file_bytes, max_line_bytes };  // may only be lowered

std::string   read_candidate_list_file(const std::filesystem::path&, const CandidateListLimits& = {});
CandidateList parse_candidate_list(std::string bytes, const SearchSpace&,
                                   const std::map<std::string, std::string>& fixed_inputs = {},
                                   const CandidateListLimits& = {});
CandidateList load_candidate_list(const std::filesystem::path&, const SearchSpace&,
                                  const std::map<std::string, std::string>& fixed_inputs = {},
                                  const CandidateListLimits& = {});   // read + parse

class CandidateList {              // immutable, safe to read from many threads
    std::uint64_t size() const;                 // N >= 1
    Candidate at(std::uint64_t position) const; // Candidate::id == position; hpo_invariant past the end
    const std::string& source_sha256() const;   // SHA-256 of the exact file bytes
    const std::string& list_sha256() const;     // see "Digests"
};
class CandidateListCursor {        // coordinator thread only; list must outlive it
    explicit CandidateListCursor(const CandidateList&);
    std::optional<Candidate> next();            // occurrence 0..N-1 in order, then nullopt forever
    std::uint64_t position() const;
};
class CandidateCoverage {          // NOT synchronized, see "Coverage"
    explicit CandidateCoverage(std::uint64_t count);   // 1..50,000, else hpo_invariant
    void record(std::uint64_t position, bool scored);  // out of range or twice: hpo_invariant
    std::uint64_t count() const, evaluated() const, scored() const;
    bool complete() const;                      // evaluated == count; says nothing about scores
    bool terminal(std::uint64_t position) const;
    std::vector<std::pair<std::uint64_t, std::uint64_t>> missing_ranges() const; // ascending, inclusive
};
std::string candidate_list_json(const CandidateList&, const CandidateCoverage&);
void require_candidate_list_settings(bool sampler_selected, bool file_given, CandidatePolicy,
                                     PrunerKind, std::uint64_t no_improvement_trials,
                                     bool warm_start_given);
void require_candidate_list_budget(std::uint64_t explicit_budget /* 0 = not given */, std::uint64_t count);
```

## Admission contract

* The file is opened once, read-only, `O_NONBLOCK`; the **descriptor** is `fstat`ed and must be a
  regular file (FIFO, directory, device, socket refused before any read). Symlinks are resolved by
  the OS; the identity of the input is the bytes read. No path, URL, environment or include
  expansion, no credential. A file that grows while it is read is still capped.
* UTF-8 JSON Lines. Each line is one flat object whose keys are exactly the search-dimension names
  and whose values are scalars. No header, comment or blank line; one final LF is optional. A BOM is
  not JSON and is refused. A trailing CR is JSON whitespace, so CRLF files are accepted (their
  `source_sha256` differs, their `list_sha256` does not). Strict UTF-8 is checked per line (no
  overlong forms, no surrogates, nothing above U+10FFFF) before the JSON parser runs; the parser
  adds depth 32, duplicate-key refusal and the RFC number grammar (no NaN, Infinity, `+1`, `.5`,
  `1.`, `01`).
* Limits, all inclusive and all checked before the plugin or dataset loads: 50,000 occurrences,
  32 MiB file, 64 KiB per line (excluding the LF), at least one occurrence. They bound **input
  only**: they do not bound output size, resident memory, or whether a wide result can be
  re-imported (the importer refuses documents above 256 MiB).
* Order of checks, so the first failure is deterministic: limits sanity, fixed-input overlap, empty
  list, file size, then each line in file order (occurrence count, line length, UTF-8, JSON,
  vector against the space). The whole file is validated before anything is returned.
* Vector rules are the importer's (`load_json_warm_history`), applied in the same order: a real
  dimension takes any JSON number as binary64 (`5` and `5.0` alike); any other dimension takes the
  scalar its spelling denotes (no `.`/`e`/`E` is int64, else real); a number that is not a declared
  categorical choice may still match a declared **real** choice (`1` matches `1.0`, `1.0` does not
  match integer `1`); `SearchSpace::is_valid`; then, if `finite_cardinality()` is known,
  `candidate_at(candidate_ordinal(v))` (so a stepped real must be the exact decoded lattice value,
  exactly as the importer demands); if the cardinality overflows or is unbounded, no
  canonicalization, as in the importer. In that unbounded case `-0.0` on a real dimension becomes
  `+0.0`, so the text handed to the strategy matches the digest (which treats the two as equal).
  Every list C admits is therefore importable, and `importer_parity()` checks that equivalence
  against the real importer on a table of spellings.
* Duplicates are **kept**: occurrence `i` has trial id `i` whatever its neighbours are. Nothing is
  deduplicated or fanned out.

### Error taxonomy (existing registered codes and reasons only)

| Condition | Code | `reason` | Exit |
|---|---|---|---|
| empty/NUL path, cannot open, not a regular file, read failure, file or line over limit, invalid UTF-8, invalid JSON (including duplicate JSON keys), blank line | `hpo_input_file_invalid` | none | 1 |
| empty list, more than 50,000 occurrences, explicit budget differs from N | `hpo_study_spec_invalid` | `sampler` | 1 |
| line not an object, missing key, unknown key (a key naming only a fixed input is unknown), wrong type, out of bounds or off-lattice, unknown choice, non-canonical lattice point | `hpo_study_spec_invalid` | `search_space` | 1 |
| fixed input named like a search dimension | `hpo_study_spec_invalid` | `input` | 1 |
| `--sampler candidates` without `--candidates` or the reverse, non-default candidate policy, pruner other than none, positive no-improvement patience | `hpo_cli_usage` | none | 1 |
| `--warm-start` with `--sampler candidates` | `hpo_warm_start_rejected` (`WarmStartError`) | none | 4 |
| limits above the maxima, coverage misuse, position out of range | `hpo_invariant` | none | 1 |

Diagnostics contain the 1-based line number, a **declared** dimension name, the numbers 50000 and
the like, and fixed words (`integer`, `real`, `boolean`, `categorical choice`). They never contain
the path, a list key, or a list value: the parser's own text (which names a duplicate key) is
replaced wholesale, and unknown keys are reported by line number only.

## Digests

* `source_sha256` = SHA-256 of the exact bytes read (hex, lowercase).
* `list_sha256` = SHA-256 of `pineforge_candidates_v1\n` followed, for each occurrence in order, by
  `candidate_key(canonical vector)` and `\n`. `candidate_key` is the importer's identity function:
  a JSON object, members in byte order of the dimension name, no whitespace, each member
  `["<type>","<value>"]` with `integer` + decimal string, `real` + 16 lowercase hex digits of the
  binary64 bits (`-0.0` as `+0.0`), `boolean` + the literal `true`/`false`, `string` + a JSON
  string (escapes only for `"` `\` `\b \f \n \r \t` and other bytes below 0x20 as `\u00xx`;
  everything else raw UTF-8). Whitespace, key order, number spelling, CRLF and the final LF do not
  change it; duplicates and order do. Goldens in the test were computed with `shasum -a 256` from
  this layout, not by the code under test. A Python writer needs exactly this recipe.

## Coverage

`CandidateCoverage` is one bit per position (782 words for 50,000), two counters, no lock. Call
`record()` only where `TrialArchive::add` already runs: both callers (`RunState::finish`, and
`check_timeouts` for the single `trial_timeout` row) hold `RunState::mutex_`, and `finish` returns
early once `timed_out_` is set, so a position is never recorded twice. Read it after
`state.shutdown()` or on the timeout path, where the archive no longer changes. `scored` means
status `ok` or `constraint_violation`; `evaluated` counts every terminal status including
`trial_timeout`. `complete` means every position has a terminal row, not that every score succeeded.
Coverage does not depend on `--trials-out`, so `best-k`/`none` and fatal partial results stay
correct.

`candidate_list_json` renders one line, value of the C-only top-level `candidate_list` key:
`{"format":"pineforge_candidates_v1","source_sha256":"…","list_sha256":"…","count":N,"evaluated":M,"scored":S,"complete":true|false,"unevaluated_ranges":[[a,b],…]}`.

## Integration lane: exact hooks (main.cpp, CMake, Python are NOT touched here)

All line numbers are `main.cpp` at `d2f83326`.

1. **Flags** (`Options` ~67, `parse_options` ~386, print_help ~277). Add `std::filesystem::path
   candidates;` and a `std::shared_ptr<const pfh::detail::CandidateList> candidate_list;` to
   `Options`. Accept `candidates` in the `--sampler` list (~389-391); add `--candidates FILE`.
   After the existing cross-checks (~595-605) call
   `require_candidate_list_settings(out.sampler == "candidates", !out.candidates.empty(),
   out.candidate_policy, out.pruner, out.no_improvement_trials, !out.warm_start.empty())`.
   `--max-trials` may be omitted for candidates; do not add it to the "requires --max-trials or
   --max-wall-seconds" rule (~590). The existing rule at ~599 already refuses a non-default policy
   for samplers other than tpe/grid; the leaf repeats it so the refusal is testable natively.
2. **Admission** in `run()` after the warm-start block (~1912) and before the plugin/dataset load
   (~1960):
   `auto list = load_candidate_list(options.candidates, space, options.fixed_inputs);
   require_candidate_list_budget(options.max_trials, list.size()); options.max_trials = list.size();
   options.candidate_list = std::make_shared<const CandidateList>(std::move(list));`
   Setting `max_trials` makes `worker_count` (~1998), `submit_batch` and `trials_requested`
   (~1427) correct unchanged. A zero-row run cannot happen: N >= 1.
3. **Proposer** in the sampler branch (~2094-2161), before the grid/random `else`:
   `CandidateListCursor cursor(*options.candidate_list); evaluate_batches([&]() ->
   std::optional<pfh::Candidate> { return cursor.next(); }, [](const TrialRecord&) {});`
   Trial ids are the positions, `evaluate_candidate` is untouched, and the existing external
   cancel/deadline/watchdog/output-error paths apply as they are.
4. **Metadata**: give `TrialArchive` an optional `CandidateCoverage` (constructed with N only for
   `candidates`) and, in `add()` (~897), `coverage.record(record.trial_id, record.status == "ok" ||
   record.status == "constraint_violation")` before the retention logic. In `render_results`
   (~1384) emit `,\n  "candidate_list": ` + `candidate_list_json(...)` after the `early_stop`
   block (~1625) and only for `candidates`; use `candidate_list_implementation` for
   `sampler_implementation` (~1403). No list field is added to ordinary trial rows, and runs
   without the new flags must keep today's bytes.
5. **`stop_reason`** (~1436): an ordinary exhausted list should read `trial_budget_reached`. The
   existing precedence puts `search_space_exhausted` first when the list happens to cover a whole
   finite space; decide whether `candidates` should skip that branch. Errors stay distinguishable
   through trial statuses, the process failure object and `candidate_list.complete`.
6. **CMake** (`tests/CMakeLists.txt`, next to `pineforge_hpo_test_warm_binary`):
   `add_executable(pineforge_hpo_test_candidate_list test_candidate_list.cpp)`;
   `target_link_libraries(... PRIVATE PineForgeHPO::core)`; `-Wall -Wextra -Wpedantic`;
   `add_test(NAME pineforge_hpo_candidate_list COMMAND pineforge_hpo_test_candidate_list)`.

### Python / StudySpec route (release scope; not done here)

`cli.py:653` and `study_spec.py:603` list the allowed `sampler.kind` values; `cli.py:708-713`
always passes `--sampler KIND --candidate-policy POLICY --max-trials TRIALS --seed SEED`.
For `kind: "candidates"`: add `candidates` (a path, resolved to an absolute path before the
subprocess call as `--trials-file` is) to the sampler keys; make `trials` optional (absent: omit
`--max-trials`; present: pass it and let native refuse a mismatch with N); require
`candidate_policy` `sampler_default` (which the command line already passes) and no
`sampler.config`; keep `seed` as recorded-only. Python must **not** read or validate the list
itself: native admission is the single authority and its failure document is relayed unchanged. A
later `candidates.py` writer must reproduce `list_sha256` from the layout above and share a fixture
with the native golden.

## Not guaranteed

No billing statement (an invalid list is refused before any trial, but cost is the app's concern).
No whole-process memory bound: the file (up to 32 MiB) is held while it is parsed, the canonical
vectors are held flat (about 40 bytes per scalar) and the list digest is built from a transient
buffer that is a small multiple of the file size, because the in-tree SHA-256 is one-shot. No claim
that a wide result is importable or that warm import is universally cheap. `std::stod` is
locale-sensitive in the importer and here alike.

## Tests pending (all unexecuted)

`tests/test_candidate_list.cpp`, run on spot: golden digests (order, duplicates, spelling, CRLF,
no final LF); positional ids and cursor; canonical spellings (`5`/`5.0`/`5e0`, zero spellings,
int-to-real choice fallback, UTF-8 versus `\u` escapes); an invalid-vector table (about 30 cases);
JSON, UTF-8, BOM, NUL, surrogate, duplicate-key and depth refusals; exact and over limits at the
real maxima (50,000 / 50,001 lines, 65,536 / 65,537-byte lines, 32 MiB / 32 MiB + 1 file) and with
tightened limits; paths and file types (empty, missing, directory, FIFO, `/dev/null`, `/dev/zero`,
symlink, dangling symlink, NUL path, unreadable); fixed-input conflict ordering; importer parity on
mixed, finite and overflowing spaces (about 100 cases); settings and budget refusals; coverage
bitmap (word boundaries, gaps, complete with zero scored, misuse) and the metadata block. Every
refusal also asserts the typed code, the reason and the absence of path, key and value text.
Suggested spot command after the integration lane registers the target:
`ctest --test-dir build -R pineforge_hpo_candidate_list --output-on-failure`.

## Unresolved questions for AR/TOP

1. Key names of the `candidate_list` block: the pin says "missing-position ranges"; the proposal
   used `unevaluated_ranges` and so does `candidate_list_json`.
2. A stepped real spelled near the lattice (`0.3` on a `0.1` grid, whose decoded value is
   `0.30000000000000004`) is refused in a finite space, exactly as the importer refuses it, while
   an unbounded space accepts it within the `RealDimension::contains` tolerance. Snapping to the
   lattice would be friendlier but goes beyond the importer's rule.
3. `-0.0` is normalized to `+0.0` only for reals the importer would not canonicalize.
4. Symlinks are followed and CRLF is accepted; a BOM is refused.
5. `stop_reason` precedence for a list that covers a whole finite space (hook 5).
6. The settings helper throws `hpo_cli_usage` itself; main.cpp may prefer its own `usage_error`.
