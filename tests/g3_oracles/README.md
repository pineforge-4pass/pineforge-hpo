# G3 oracles

Independent exact-arithmetic checkers for the three post hoc analysis modules
`holdout_compare`, `dsr` and `walkforward`. Each has two phases: the checker against
fixture pins, then the module against the checker. Their comparison and coverage limits are
listed in their sections below; agreement covers only the quantities named as compared.

Python 3.11 or newer, standard library only; run the commands from the repository root.
`--cases N` sets the number of seeded random cases, with defaults and minimums given below.
`--seed S` sets the random seed; all three default to 20261012.

## How every checker is built

- **Phase 1 needs no module.** Each checker uses embedded fixture inputs and expected pins,
  checks its controls and prepares seeded random cases without importing package code.
  Expected numbers are never taken from the module.
- **Phase 2 imports the module only after phase 1 has finished.** An import-boundary check
  precedes the package import. The module receives the same case inputs and its output is
  compared with the oracle's answers. No module comparison is counted in phase 1.
- Each script starts with a harness self-test that checks that equality distinguishes
  different values and that the ledger records a planted disagreement. Its verdicts use
  primitive `is` checks, so the comparison helper cannot vouch for itself.
  `harness self-test passed` precedes phase 1.
- Every comparison section prints `A comparisons made, B expected`; unequal counts fail.
  The `SUMMARY` lists `fixture`, `internal`, `control` and `module` counts and failures.
  The numerical comparison rules and tolerances differ by oracle and are stated below.
- `--no-module` runs phase 1 only and imports no package. Successful phase 1 prints
  `RESULT: PHASE 1 ONLY (exit 3)` with an explicit statement that the module was not compared.
  It exits 3 and is never a pass. A failed phase 1 still exits 1.

A full passing run prints `RESULT: PASS` and exits 0 after agreement with fixture pins
and the compared module fields. Failed comparisons or accounting checks give exit 1 and
appear in the failure list. Usage errors give argparse's exit 2. Walk-forward also uses
exit 2 for its unavailable module, as described below. Read the verdict as well as the exit
code: only exit 0 with `RESULT: PASS` reports full agreement.

The scripts write no files. Each sets `sys.dont_write_bytecode` before importing the package,
so the imports create no bytecode caches. They print their comparisons and verdicts.

## `holdout_oracle.py`

Independent checker for the post hoc comparison of two finished studies through
`holdout_compare` in `pineforge_hpo.holdout`.

```text
python3 tests/g3_oracles/holdout_oracle.py [--no-module] [--cases N] [--seed S]
```

It recomputes built-in worked fixtures and compares them with typed expected values.
Seeded random cases compare whole documents,
including Spearman against a literal successful tolerance verdict.

Comparisons are exact and type-strict for numbers, except Spearman: its tolerance is one ulp
at the larger of the two magnitudes,
symmetrically in either argument order, including negative values at powers of two.
Its exact terms and rank vectors are compared without tolerance.

Row accounting counts eligible rows plus the lengths of exclusion ID lists, without
using `rows_excluded`. Worked fixture checks compare typed pinned counts with rows read
in phase 1 and the module's counts with the oracle's row totals in phase 2. Random
phase-1 checks use input row counts; phase-2 checks use the module's eligibility and
exclusion numbers against the oracle's row totals.

A traceback indicates a script defect or an unexpected module exception.

- **Recomputes:** per-source row counts, eligibility and exclusion reasons with IDs,
  eligible row and vector counts, selection ranks, followed vectors, the matched and
  unmatched join with its reasons and `holdout_ids`, pool size and mid-ranks,
  holdout-only vectors, both correlation rank vectors, the three exact terms,
  binary64 correlation and undefined reasons, the better-half share, ancestor counts,
  admission figure and digest of all rows read.
- **Method:** the reader layer reproduces the shared reader for parsed result objects
  and lists of rows. Eligibility follows the same sequential per-row ladder as the
  module; it is not an independent route. Arithmetic and ranks are the independent
  routes. Selection ranks count vectors strictly before each vector, with equal
  values ordered by representative trial ID. Pool and correlation mid-ranks count
  strictly better and not-worse values, giving `(p + q) / 2` as a `Fraction`.
  Sorting routes are computed alongside these enumeration routes and must agree.
  Correlation terms use two-pass deviations multiplied by the matched count,
  compared with one-pass terms. The oracle takes decimal square roots of its exact
  ratio's numerator and denominator at 50 significant digits, divides them and rounds
  once to binary64, applying the sign of `Sxy`. The module first rounds the exact
  ratio to binary64, then takes `math.sqrt`, explaining the one-ulp tolerance.
  Join keys are rebuilt for categorical, boolean, integer and real dimensions.
  The digest rebuilds compact, key-sorted, ASCII JSON of each row's source index,
  trial ID, status, value and vector key, hashes each row, sorts the digests and hashes
  their concatenation.
- **Fixtures:** six selection rows and five holdout rows at `k` = 5, 7 and 3; a
  two-vector named-metric case under minimize; four vectors with a tie straddling an
  even median cut; named-metric exclusions; two results recording a space without
  result-level directions; constant holdout ranks; nothing matched; agreeing
  repeated vectors; a holdout ancestor that repeats no selection ID; a pool larger
  than the matched set; and constant selection ranks. Refusal fixtures include
  conflicting repeats in both sources and in the holdout alone. Seeded random
  instances default to 120 cases. `--cases` must be at least 30.
- **Controls:** five, all one-sided. The first four mutate only the oracle's input
  copy: reverse a selection tie, nudge one holdout value by one ulp, edit one join key,
  or raise one selection value to reorder the followed set. The second and fourth
  check that pool size remains unchanged; the third changes it. The fifth changes
  only the oracle's computed Spearman by two ulps away from the comparison value.
  It uses the same document comparison as random cases, after checking that the
  unshifted documents agree. Phase 1 compares mutated and clean oracle documents;
  phase 2 feeds the module unmutated inputs. Detected mutations print `AGREE`
  because the comparison detects the expected disagreement. An undetected mutation
  prints `DISAGREE` and fails the run.

Coverage limits:

1. Never established by these comparisons: the full order of refusal checks; the
   choice of statistic (Pearson over mid-ranks, median cut and matched denominator);
   whether intermediate sums use exact rationals or binary64 at large sizes; path
   ancestor counting and file layout dispatch; keys for stepped real, log or
   log-integer dimensions; the digest's key order for object cells and ASCII encoding
   for non-ASCII strings; or the correctness of interpretation wording. The
   interpretation is compared only as a constant. Rank fixtures are too small to
   distinguish exact sums from binary64 sums; the one-pass rank arithmetic agrees
   while `4n^4/3 <= 2^53`, up to about `n = 9 * 10^3`.
2. Phase 2's explicit refusal table sends these cases to the module and compares
   code, IDs and names: missing `k` before an invalid metric; missing named-metric
   direction before invalid `k`; a non-string metric with `direction=None`; a
   pruned row's object cell with mixed-type keys that the canonical writer cannot
   sort; a non-finite difference; disagreeing selection repeats; conflicting groups
   in both sources, naming every eligible row of the selection's conflicting groups
   only; holdout-only conflicts,
   naming every eligible row of those groups; overlapping holdout ancestors; a
   within-source backtest-counter disagreement; parameter names outside the space;
   and mismatched recorded spaces.
   The admission boundary at 27 versus 26 is also compared, by status and code.
   Random cases may reach additional refusals, but do not guarantee those shapes.
   The mixed-type-key case exercises the sorted writer's cell writability check,
   without establishing the digest's key order for writable object cells.
   Both phases compare code, IDs and names for the two argument-order pairs, the
   non-string-metric case and the mixed-type-key cell.
3. Phase 1 alone pins additional refusals: missing and invalid arguments, an invalid
   source type, incomplete rows, a missing space, unknown metric names, no eligible
   selection row, an ok row's string cell, a nested NaN cell, a set cell, and an
   integer too large for binary64. Row and metric defects compare code, IDs and
   names; single-argument, invalid-source-type, incomplete-row, missing-space,
   no-eligible-row and resource refusals compare code only. The non-refusal of a
   pruned row's string cell is pinned as an exclusion. These phase-1 checks are not
   module-side refusal coverage.
4. Random inputs have two dimensions, a categorical with four choices and an
   integer with three values; one to seven rows per source; values 0 to 3; objective
   or a named metric, sometimes absent or null; and statuses ok, pruned,
   constraint_violation, engine_error, partial and trial_timeout. No case reaches
   continuous, stepped or log dimensions, a boolean dimension, negative zero,
   values outside 0 to 3, row-recorded spaces, path sources or large rank sums.
   No fixture or random input uses objective_error, constraint_error or trial_error.
5. Reproduction limits: the reader layer reproduces the shared reader; eligibility
   reproduces the module's ladder; join keys and digest bytes reproduce their stated
   recipes; and the refusal order follows the stated check sequence: missing
   before invalid; `metric`, `k`, `direction`, `max_work`, then the type of `space`.
   Agreement on these quantities checks that the recipe is implemented as stated,
   without independent evidence for the analysis itself.
6. The module receives parsed result objects or lists of rows. The script does not
   build a `StudySpec`; the oracle accepts passed spaces only as recorded-space
   dictionaries and would refuse a `StudySpec` where the module accepts one.

## `dsr_oracle.py`

```text
python3 tests/g3_oracles/dsr_oracle.py [--no-module] [--cases N] [--seed S]
```

Independent checker for the deflated Sharpe ratio over recorded statistics of
finished studies through `pineforge_hpo.dsr`.
This oracle recomputes V and den with exact rationals and the normal quantities
with decimal series.

- **Fixture inputs.** Fixture inputs and exact or tabular pins are
  embedded with their worked derivations. The checker compares every
  declared pin, checks its controls, and prepares seeded random cases before
  the module import. A package-import check precedes and
  follows this phase. Fixtures have plain descriptive labels; no fixture file is
  read. The checker uses embedded inputs to compute its answers.
- **Trial counts.** Default N is every row read, including pruned, failed,
  unavailable and warm-start rows. Eligible rows supply the variance and the
  selected row's statistics. A caller's N must be at least the rows read.
  `n_trials_source` is `rows_read` or `caller`. Fixtures with exclusions use N
  equal to the rows read. Phase 1 prints their computed values in
  **`PINNED VALUES FOR TESTS`**, including N, rounded V and den, exact
  fractions, binary64 literals and 40-digit decimal readings. This block is
  computed evidence for copying into tests after successful phase 1; printing it is
  not counted as a comparison. It is printed only when no failure is recorded.
  Normal-table pins use hand-derived values and absolute tolerance `5e-4`.
- **Recomputes.** The selected row's SR, T, skew and raw kurtosis; sample V;
  den; N and its source; `sr0`, `z`, `dsr`; eligible ids; excluded reasons,
  counts and ids; distinct type-preserving series status entries;
  `selected_is_max`; and the admission boundary `rows_read + rows_eligible`.
  Public refusal cases compare code, ids and names, except the reader's
  input-invalid and incomplete-source cases, whose pins specify code only.
  An unwritable series entry in any row is refused before exclusions apply,
  with its trial id and entry key pinned. This includes an object with mixed-type
  keys that the key-sorted writer cannot sort. Several offending entries report
  the union of trial ids in ascending order and the keys in alphabetical order.
  A writable odd status entry is excluded and appears in the reported statuses.
  Value entries accept integers beyond binary64 or the interpreter's integer
  text limit: an `ok` row with an overflowing value is `returns_unavailable`,
  while a pruned row keeps its status exclusion. An integer beyond the
  interpreter's integer-to-text limit in the status entry is refused; an integer
  that is large but writable there is a status that is not ok. The pruned-skew
  example pins the digest against literal JSON preimages containing
  `["other", "<unwritable>"]`, using the interpreter's default 4300-digit limit.
  This digest check reproduces the documented recipe.
- **Method.** V is the centred exact `Fraction` sum of squared deviations,
  divided by the eligible count minus one. Den is an exact polynomial over
  the recorded binary64 dyadics. Each rational is rounded once. Phi uses a
  decimal Maclaurin integral series for `|z| <= 3` and a direct complementary
  integral series above it. The complementary series integrates quarter-unit
  panels of the entire exponential power series and uses Mills' bound for the
  remaining tail; it does not subtract a negative-tail probability from one.
  Machin's arctangent series supplies pi. Arithmetic uses 80 guard digits
  (90 for pi), with normal inverses bisected on `[-40, 40]` for 160 steps and
  results rounded to 40 digits. Beyond `|z| = 40`, Mills' bound is below half
  the smallest binary64 subnormal, so the reported saturation is independently
  justified. The normal implementation uses no normal-distribution library,
  `erf`, `erfc` or module helper.
  Standalone normal-table references pin Phi(-3), Phi(-10) and Phi_inv(0.975)
  at relative tolerance `1e-9`. Symmetry gives the negative CDF values from
  Phi(3) and the direct survival value Q(10); a quadratic inverse interpolation
  about the table entry at 1.96 gives the 0.975 quantile. The table entries and
  derivation are written next to the checks, independently of the decimal series.
- **Rounding replay.** The pinned binary64 G and E are inputs. The checker
  performs each rational operation and binary64 conversion separately when
  forming p1 and p2, reproducing the specified multiplication, division and
  subtraction order. It compares these arguments internally with ordinary
  binary64 operations, including N = 3 and the upper bound `2**50`. Differences
  from the exact unrounded arguments are printed as information only. The
  arguments are not public result fields and are not compared with the module.
- **Bounds.** Module V and den, the selected statistics, counts, ids, reason
  strings and labels are compared exactly, with type checks and binary64 bit
  checks. The result must contain exactly the 25 public keys in their specified
  order; excluded-object key order is checked. `sr0`, `z` and `dsr` must each
  have type `float`. `sr0` uses 8 ulps of the oracle value;
  `z` uses `8*ulp(SR0)*sqrt(T-1)/sqrt(den) + 4*ulp(abs(z))`; `dsr` uses
  `phi(z)*tol_z + 1e-15`. The oracle uses the same rounded square roots as the
  module; it does not claim to check the platform square root.
- **Controls.** The harness self-test has additional checks. It also verifies type and bit
  differences, finite-value tolerance comparisons
  and missing-comparison accounting. Before the module is loaded, the worked case's
  first eligible Sharpe is increased by `1e-9` in a copy: V and `sr0` decrease,
  z and DSR increase, each beyond its comparison bound. A copied oracle answer
  is recomputed with only the `(1-G)*q1` term. Another copy receives the raw
  one-pass binary64 variance of `0.1, 0.2, 0.3`, which must fail exact comparison
  and equal the hand-derived value five ulps below the exact rounding. A copied
  argument pair receives single-rounded p1 at N = 3, which must differ by one
  ulp. Neither side of any module comparison is mutated. Two-pass binary64 V
  and the binary64 denominator are printed as non-separating information.
  Every fixed source is snapshotted before oracle evaluation and compared after
  the controls, retaining types, key order and non-finite spellings.
  Random sources, including redraws and fallbacks, are also snapshotted before
  evaluation and checked afterward.
- **Fixtures and refusals.** All numeric fixtures from the worked case,
  caller-count example, exclusion ladder, selected-first example, rational
  identities, integer and boolean examples, positive and negative saturation,
  eligible-only maximum, adjacent-float maximum and two-series example are
  included. Supplemental cases cover warm-start counting, type-distinct status
  entries, sparse trial ids and work equality. Cases cover all twelve public
  refusal codes; additional cases cover positive rational V or den rounding to zero and
  caller counts equal to the eligible count but below rows read, a variance
  overflowing binary64 and a non-finite z with a positive rounded den. Strict JSON
  NaN, Infinity, overflowing-number and duplicate-key payloads are delivered
  through bounded pipes, alongside a valid unmodified payload that must return
  the full compared result. Count-before-selection, inconsistent-count-before-work
  and work-before-zero-variance cases pin refusal order. A single pruned row with
  a NaN series status pins input refusal before the no-eligible-rows refusal.
  Phase 2 needs a POSIX host exposing file descriptors through the root-level
  `dev/fd` interface for those strict-file checks.
  Phase 1 needs no pipes.
- **Random cases.** `--cases` is positive, default 60.
  Each case has 2–6 eligible rows with Sharpe from
  `{-2, -1, -0.5, 0, 0.5, 1, 1.5, 2}`, count 2–12, skew in `[-3, 3]`, raw
  kurtosis in `[1.5, 12]`, and four additional rows covering each exclusion
  family. Trial ids are drawn without replacement from a sparse set, allowing
  excluded ids to sort between eligible ids. Selection is uniform among eligible
  trial ids. Alternating cases leave N at rows read or draw a caller count from
  rows read through rows read plus four.
  A refused case is retained and compared by code, ids and names. An admitted
  case outside `[-3, 3]` is redrawn up to 200 times; exhaustion replaces it with
  the fixed worked case, still compared, and reports the replacement count.
  Admitted random cases also compare every covered field after row shuffling.
  Random cases exercise bar statistics and small N; the monthly fixture covers
  the other series. The exact probability replay reaches the upper N bound,
  but the full statistic is not compared there.
- **Accounting and verdicts.** Module counts are determined from the frozen oracle outcomes,
  never from the
  module's returned keys. Missing, extra or reordered keys fail comparisons.
  An exception is reported as a failure.

**Coverage limits / not compared.** The values of `input_sha256` except the
oversized-integer digest case; `interpretation`, `contract`, `status`, `algorithm`
and `series`; the module's Phi and inverse directly;
refusal detail text and refusal document shape; the private T < 2 guard that
eligible rows cannot reach; all source-error variants beyond the named cases;
file splitting and repeated-call invariance; the platform square root; and a
saturation versus a clamp giving the same number. The refusal priority and
eligibility priority reproduce the documented ordered rules. The one
Fraction-to-binary64 rounding is shared. An exact V or den agreement checks
the definition and rounding rule; it does not establish the module's internal
arithmetic route against every binary64 implementation. The raw one-pass
control separates that specific wrong variance route only. The digest comparison
is a reproduction of the documented recipe. The fixed interpretation text is
not recomputed.

Also outside the exercised inputs: full-statistic comparison at `N = 2**50`,
large T, integer-cell rounding above `2**53`, context mismatches in backtest
counters, and trial statuses other than `ok`, `pruned` and `engine_error`.

The statistic is conditional on recorded moments and the stated trial count.
It is an in-sample deflated reading, without a threshold or an out-of-sample
claim.

## `walkforward_oracle.py`

```text
python3 tests/g3_oracles/walkforward_oracle.py [--no-module] [--cases N] [--seed S]
```

Checks `pineforge_hpo.walkforward`.
v1 analyses finished studies; native in-loop walk-forward comes later.

* **Recomputes** each supplied window's eligible rows, exclusions and their key
  order, selected row and recorded parameters; every carry-over entry, including
  absent and ineligible carrying rows, duplicate handling, `better`, `equal` and
  exact mid-rank; the supplied and never-run lists, chain breaks, stability values,
  distinct counts and extremes; and the integer admission figure. Fixture digests
  are rebuilt from literal preimage texts.
* **Method.** Selection takes the eligible row for which there is no strictly
  better eligible value and no equal value with a lower id. It never sorts on a
  direction-dependent sign. Every metric number first becomes its exact binary64
  rational; mid-ranks are `Fraction` values and a module float is converted to
  `Fraction` for comparison, without tolerance. Lookup uses an index of numeric
  JSON keys normalised by dimension kind, independently of the module's typed
  scalar keys. Stability counts sets of kind-tagged values, preserves the recorded
  values, and compares numeric extremes exactly: an int and a float compare by
  exact value, and the first of equal values is kept.
  Dimension objects are compared as compact, key-sorted JSON text, so integer
  `1`, float `1.0`, `-0.0` and `0.0` remain distinguishable in the report. The
  recursive comparison also checks float zero signs in selected parameters,
  carry-over parameters and recorded values. Rational conversion is used for
  arithmetic comparisons; reported values retain the original binary64 zero sign.
  Vector-key wire strings are excluded from both documents before comparison.
  The admission allowance is counted by summing triangular rows in integer arithmetic.
* **Digest.** The twelve literal preimages of the worked objective fixture are
  written in the script, with independent literal replacements for changed
  statuses and literal entries for the smaller and recorded-metric fixtures.
  Each text is hashed, the hexadecimal digests are sorted, concatenated and
  decoded to bytes, then hashed again. Hex sorting gives the raw-byte ordering
  specified by the digest recipe in `docs/walkforward.md`. Phase 1 checks the
  parsed literal columns against independently read fixture columns and
  establishes that the twelve digests'
  read order differs from their sorted order. There is no hand-derived SHA-256
  number to compare in phase 1, and hashing the same texts twice is not counted
  as evidence. Phase 2 compares these literal-derived hashes with module digests.
* **Fixtures.** Twenty-two accepted cases cover the three-window worked example;
  absent and pruned carrying rows; equal carried duplicates and conflicting
  duplicates unrelated to the carried key; one or two supplied windows, including
  a never-run middle window; a supplied middle window without a selection, also
  with reversed row order; a target without eligible rows; both directions for
  the recorded profit metric, absent and null cells and exclusion key order;
  recorded real values represented by integers and floats, signed zeros and
  earliest tied extremes; exact integer and real extremes above `2**53`, including
  different extremes with one real typed identity; admission bounds equal to and
  above the figure; different contexts across windows; equal recorded hashes;
  a hash without a recorded space and with nothing else to compare it to; and a
  one-window plan.
  Pins are hand-derived independently of the oracle's computation from the
  rules in `docs/walkforward.md`; additional derivations are identified below.
  Thirty-five one-defect cases cover every refusal code, comparing the code,
  ascending ids and the first `names` entry. Two additional cases compare all
  unknown-window names for sources labelled `1` and `"1"` in both mapping orders:
  labels with the same text are ordered by the name of their type, so both cases
  expect `(1, "1")`. Four additional phase-2 calls check omitted required arguments
  against `TypeError`.
* **Random cases.** `--cases` defaults to 100 and must be at least 30.
  Random plans have 3–8 labels in random caller order,
  randomly absent windows, supplied empty windows, 3–12 rows in the first supplied
  window and 0–12 rows in each later supplied window, exact eligible ties,
  duplicate vectors with consistent values, all nine statuses,
  and alternating maximize/minimize directions. Small legal integer, stepped real
  and boolean dimensions are used, sometimes with a string categorical dimension.
  Cells include adjacent doubles and signed zeros. Coverage checks examine the
  inputs for both directions, gaps, empty windows, all statuses and ties. Phase 1
  checks row/mapping permutation invariance. Phase 2 compares each original and
  shuffled case with the independently computed document.
* **One-sided controls.** Each changes a private copy, leaving its comparison
  reference untouched: change the second window's last objective from 5 to 4
  (the tied selection becomes id 0); replace a mid-rank of
  2.5 with ordinal 1 (an exact difference of 1.5); remove a label from one literal
  digest preimage; shorten a two-label never-run list; and replace the triangular
  allowance with an ordered-pair allowance (work becomes 36 instead of 27).
  All five must disagree with the unchanged reference. They are implemented in
  phase 1 against the pins and literal-derived reference, and in phase 2 against
  the unmodified module results. These five controls make eight assertions in
  each phase. A separate phase-1 control changes the sign of a target's carried
  zero. Phase 2 also plants an ordinal rank and a carried zero-sign change in
  separate deep copies of module documents. Each is sent through the full
  document comparator with an isolated ledger, which must record exactly the
  planted disagreement and complete its three comparisons. Those deliberately
  failing probe comparisons contribute one control check each to the main ledger.
  The changed three-way tie input is also sent to the module and compared with
  its independently recomputed oracle document.
* **Harness and accounting.** The startup self-test plants differences in ranks,
  adjacent floats, types and nested containers, checks byte distinctions for
  signed zero, and verifies that the ledger records both a disagreement and a
  missing comparison. Fixture totals derive from a tuple of twenty-two expected
  fixture names; refusal totals derive from fourteen expected codes and their
  per-code multiplicities. Assertions check the generated names, code set and
  multiplicities. Random counts come from the requested `--cases` value.

The expected counts below describe the program, not an observed execution. With
`F = len(FIXTURE_NAMES)`, `R = sum(REFUSAL_MULTIPLICITIES)` and `N = --cases`,
phase 1 has `2F + R + 2 = 81` fixture, `N + 10` internal and 9 control comparisons.
Phase 2 adds one internal import-boundary check, 10 controls and
`3F + R + 16 + 6N = 117 + 6N` module comparisons. The exact interpretation wording is excluded;
its presence as a nonempty string is a separate phase-2 check.
Each phase also makes `U = len(unknown_label_cases())` comparisons for unknown
labels with equal text: phase 1 adds `U` fixture comparisons and phase 2 adds `U`
module comparisons.

Only `ModuleNotFoundError` naming `pineforge_hpo.walkforward` is printed
as `MODULE UNAVAILABLE` after phase 1 and exits 2. Other import failures exit 1 with the error
message.

**Additional independently derived pins:**

These expectations are readings of the analysis rules rather than explicit
worked-example pins:

* The first refusal name is `windows` for `windows=["w1", "w1"]` and
  `windows=["w1", ""]`.
* Parameter-fault refusals and non-finite or unwritable metric-cell refusals
  carry the offending row id and begin their names with its window label.
* Additional stability columns for the supplied middle window without selection,
  unrelated conflicting duplicates, absent carrying vector and pruned carrying
  row follow the selected vectors, their distinct counts and numeric extremes.
* For two windows that differ from the reference hash, the ids were independently
  derived as `(0, 0, 1, 1, 2, 2, 3, 3)`: one entry per differing row, repeated
  across windows. The first name is the reference window `w1`.
* Integer A values `[4611686018427387905, 4611686018427387904]` are distinct
  integers: distinct 2, minimum `4611686018427387904`, maximum
  `4611686018427387905`. Comparing through floats would tie them and keep the
  first value as the minimum.
* Real B values `[9007199254740993, 9007199254740992.0]` share one binary64
  identity because the integer rounds ties-to-even to `9007199254740992.0`:
  distinct 1. Exact comparison gives minimum float `9007199254740992.0` and
  maximum int `9007199254740993`. Carry-over finds the target's row with rank 1.
  Each large-number fixture selects its only row in each window, with two read
  and eligible rows, no exclusions, and work `2 + 0 + 1 = 3`.

**Coverage limits / not compared:**

1. Exact interpretation wording; no comparison with the module's own constant is
   counted. Only its presence as nonempty text is checked.
2. Refusal order between simultaneous defects, values inside `detail`, and `names`
   entries after the first, apart from the unknown-label tie cases. Other cases
   carry one defect and compare code, ids and first name only.
3. Unprovoked reader faults: files, bad UTF-8, duplicate JSON keys, NaN/Infinity or
   overflow tokens, and the reader's complete validation rules. Sources here are
   inline parsed lists/results; no `StudySpec` conversion is exercised.
4. Random digests. Only fixture digests have independent literal preimages.
5. Non-ok cells that are strings, booleans, arrays or objects, and metrics
   containers other than objects. The non-finite and unwritable non-ok cells are
   refusal cases; their digests are never produced.
6. Recorded metric names other than the profit metric of the worked example.
   Random cases use the objective; recorded-metric exclusions and cell defects
   are covered only by the explicit fixture table.
7. Full parameter-domain validation, continuous/log spaces, or categorical
   choices mixing scalar types. Vector-key wire strings are not compared;
   independent identity evidence comes from numeric kind-normalised lookup and
   stability sets. No fixture separates two parameter values one ulp apart.
8. The direction naming convention and selection definition independently of the
   pins: both programs read the same names. The hand-derived maximize/minimize
   choices and tie pins anchor those conventions; permutation checks alone do not.
9. Native execution, dataset identity, out-of-sample claims or robustness evidence.
   A carry-over number is a value already recorded by the caller's finished study.
