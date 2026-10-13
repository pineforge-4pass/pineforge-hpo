# G2 oracles

Independent checkers for the three G2 analysis modules (`pbo.py`, `pareto.py`,
`neighbourhood.py` with `sensitivity.py`), and one real-rows end-to-end script for each. The
pins name an independent brute-force checker as the oracle for the numbers their fixtures
carry; these are those checkers. They are written by a different hand than the modules and
compute the same quantities by a different method, so that where a checker and a module agree
the agreement is evidence, and where they differ the difference is the finding.

**Status: none of these scripts has been run.** They were parse-checked only. The first run
happens on the box (not on a developer machine). Python 3.11 or newer, standard library only;
run them from the repository root. No G2 result should be called accepted before they have run
there.

The known limits of each script are listed once, under "Coverage limits" in its own section
below: the quantities it never compares (for example the SHA-256 `input_sha256` digests, which
the module tests rebuild from the documented recipe and the oracles do not re-derive, and the
fixed `interpretation` texts), the refusal codes it never provokes, the sizes and kinds of data
its random cases reach, and the places where an agreement is a reproduction and not an
independent reading. Until the limits of a script are lifted, a G2 agreement may be cited only
for a quantity that the section of that script names as recomputed or compared, and not for one
that its list marks as never compared or as a reproduction. A list can miss a quantity, and a
quantity missing from a list is no licence to cite it: what a section does not name as compared
is not covered. Selected-mode PBO and the native reducer are not in this tree.

## How every checker is built

* **Phase 1 needs no module.** The checker recomputes each pinned value from the fixture inputs
  and compares it with the value the test file pins ("fixture" lines:
  `checker=... | fixture=... [test]`). The pinned values are the hand derivations in
  `tests/python/test_*.py`; each was re-derived by hand by the author of the oracle before being
  written down, and the test that pins it is named in the output by its file, class and name
  (for example `test_pbo.py HandCountTests.test_s4_counts_and_lambdas`), never by a line number,
  because the test files move.
* **Phase 2 imports the module only after phase 1 has finished** (the script checks that nothing
  of `pineforge_hpo` was imported before), runs the module on the same inputs and compares its
  output with the checker's value ("module" lines). A checker never takes a number from the
  module.
* "internal" lines compare two routes inside the oracle (for example exact fractions against
  binary64 arithmetic, or the rule-based neighbour set against an enumeration of the whole
  product); "control" lines show that the comparison can fail and that the checker follows a
  changed input (a hand-derived perturbation of a fixture).
* Every section prints `-- <name>: A comparisons made, B expected`; the script records a
  failure when `A != B`, so a skipped or empty comparison cannot pass. A comparison is exact
  (numbers by value, no tolerance) except where a line says `ulps`. The Pareto and
  neighbourhood comparisons are also type-strict (an int is not a float: `4` and `4.0` are
  different neighbour vectors for a candidate list). The PBO comparisons compare numbers by
  value, except the three split counts, which are compared by value and by type under the
  count rule in the `pbo_oracle.py` section below.
* Each script starts with a harness self-test that exits at once if the comparison helper cannot
  tell different values apart (its verdicts use `is`, so a broken helper cannot vouch for
  itself) or if the ledger does not record a planted difference; a line
  `harness self-test passed` must precede phase 1.
* `--no-module` runs phase 1 only. The verdict then reads `RESULT: PHASE 1 ONLY (exit 3)` and
  says the module was NOT run, and the exit code is 3, not 0: a run that never compared the
  module is not a pass. Exit 1 still means a failed comparison and exit 2 a usage error.

A passing run ends with `RESULT: PASS (checker agrees with the pinned values and with the module)`
and exit code 0, every section line shows `A == B`, and the `SUMMARY` lists non-zero counts for
`fixture`, `internal`, `control` and `module`. A failing run prints `DISAGREE` lines (the first
line of each says which value differs), possibly `FAIL` lines (a section count or coverage
check), the list of failures, `RESULT: FAIL` and exit code 1. A Python traceback is a defect of
the script or an unexpected module exception, not a verdict.

Exit codes are per script and some numbers are reused: 3 is `--no-module` in the three oracles,
`--other-study` in `pareto_real_rows.py` and BLOCKED in `pbo_real_rows.py`. Exit 2 is a usage
error in every script (argparse's own code: a `usage:` message on stderr, no result line), and
in the three real-rows scripts it is also a refusal: the module's, printed as `MODULE REFUSED:`,
and in `pbo_real_rows.py` also the script's own refusal of a result that keeps only some of its
trials, printed as `REFUSED BY THE SCRIPT:`. Exit 2 is never an agreement or a disagreement,
and the line printed tells its causes apart. A wrapper must read the `RESULT:` line as well as
the exit code, and may cite an agreement only from exit 0 with `RESULT: PASS` (an oracle) or
`RESULT: AGREE` (a real-rows script).

## What the scripts write

Nothing, except where this section says so. Every script sets `sys.dont_write_bytecode` before
it imports the oracle module or the package, so no `__pycache__` appears in `tests/g2_oracles/`
or in `python/pineforge_hpo/` (starting the interpreter with `python3 -B` sets the same flag
earlier and does no harm). The three oracles, `pbo_real_rows.py` and
`neighbourhood_real_rows.py` only read and print. `pareto_real_rows.py --run` writes the result
file named by `--out` and, through `prepare_run`, the native artifacts, request records and
their locks under `--cache-dir` (the staging and temporary files of that cache are created
inside it, not in the system temporary directory). Both options are required with `--run` and
are meant to be paths in a scratch directory, so that nothing lands in the user's cache
directory (`$XDG_CACHE_HOME` or `~/.cache/pineforge/hpo`) by default. A run of `--result`
writes nothing. One thing a `--run` cannot bound: the C++ compiler that `prepare_run` starts
inherits the script's environment, and it may put its own intermediate files in the system
temporary directory (`$TMPDIR`, or `/tmp`); the script does not control that. Set `TMPDIR` to a
path in the scratch directory to keep them there too.

## Checkers

### `pbo_oracle.py`

```
python3 tests/g2_oracles/pbo_oracle.py [--no-module] [--cases N] [--seed S]
```

* **Recomputes** the number and order of the C(S, S/2) ordered splits, every split's training
  winner, its out-of-sample mid-rank, the odds `omega/(1-omega)` and `lambda`, the counts
  `n_lt0`, `n_eq0`, `n_gt0` with 1/k weights for ties, both rates (`pbo` is `rate_le0`), the
  pooled moments and the ranking statistic, the admission figure, and the zero-variance
  refusals. It also states, from the case's own data and never from the module, what a result
  says of the case itself: the layout of the blocks (`equal_blocks`, `block_counts`, from the
  interval count of every block that the case declares) and the `coverage` (the number of trials
  of the case, three times).
* **Method.** Splits: every S-bit mask scanned for popcount S/2 and Pascal's triangle (no
  `math.comb`, no `itertools.combinations`). Statistic: from the raw interval returns by the
  two-pass definition in exact rational arithmetic, compared through
  `sign(mean)*mean^2/variance` (strictly increasing in mean over sample sigma, so no square
  root and exact ties); from block moments by the within-plus-between decomposition, not a
  pairwise merge. Ranks and odds as exact fractions.
* **Calls.** The module takes its rows in parts, a list of `(declared object, rows)` pairs, with
  the declared roster, the sampler and the early stop (`docs/pbo.md`, Input and Result). A case
  here is a fixture and not a study, so every call is one part whose declared object and roster
  come from its own rows, with `early_stop` None (except the roster-label rows, which pass True
  for a study that had a stop): the call that gave the module a bare list of rows took the rows as
  the whole lineage. In the declared object `contract` and `series` are the
  pinned texts; `S`, `K` and `n_b` come from the counts of the rows (`K` is their sum); the three
  keys that no fixture carries are stated by the script, and no comparison reads them:
  `boundary_time_ms` places interval k at k milliseconds (the cases have no clock),
  `numeric_build_identity` says that no reducer build produced the moments, and
  `dataset_identity` is the name of the fixture or of the case. The roster is the ids of the rows.
* **Counts.** The oracle's `n_lt0`, `n_eq0` and `n_gt0` are exact fractions (a split with k tied
  winners weighs 1/k for each of them). An exact fraction such as 4/3 is not equal to the binary64
  that the module can hold, so comparing the exact value would report a disagreement on a correct
  result. The rule is: the expected count is `int(c)` when the exact count `c` is a whole number
  and `float(c)` otherwise (a Fraction converts with correct rounding; `docs/pbo.md`, Exactness:
  "an exact fraction converted once to binary64"), and the module's count must equal it in value
  and in type. There is no tolerance: controls in the script show that the neighbouring binary64
  and a float for a whole count both fail. The rule cannot hide a wrong count: a split counted in
  another bucket or with another tie size moves a count by at least 1/(N(N-1)), while half an ulp
  of a count is at most 1.2e-16 times `n_splits`; under the default `--oracle-limit` of
  `pbo_real_rows.py` (`n_splits` times N at most 2,000,000) the first is always the larger. The
  two rates are compared exactly, as before: both sides convert the same exact fraction once.
* **Fixtures.** `tests/python/fixtures/pbo/splits_s4_n4.json`, `tie_s2_n3.json`,
  `f4_atom_s2_n3.json` (raw series taken from the fixtures' own descriptions and checked against
  the JSON payload), the pooled-moment cases and the F5 near-constant case of `test_pbo.py`, the
  work-figure table, three zero-variance cases, four deliberately uneven block layouts that
  default D2 refuses (counts 2 and 4; 4, 3, 2 and 3; 2, 4, 8 and 4; 8, 9, 10 and 9), and three
  cases derived by hand in the script with negative means, a zero mean and returns that change
  sign (the fixture files hold none). The two fixtures of uneven but legal blocks
  (`uneven_s2_n3.json`, `uneven_s4_n3.json`) are not read; the random cases are the legal ones.
  Plus seeded random tables (`--cases`, default 60, returns from -40 to 40, block sizes that
  differ by at most one interval), compared module
  against checker only where no two trials tie exactly through different block moments (binary64
  equality of default D4 may then differ from exact equality; a tie at a zero mean is exact on
  both sides and is kept). A generated case with such a tie is set aside and replaced, so
  `--cases` unambiguous cases are always compared; the count and the attempt numbers of the
  set-aside cases are printed in a `note:` line, and the verdict stays PASS.
* **Coverage limits.**
  1. Never compared: `input_sha256`, `return_blocks_sha256`, `interpretation`, `contract`,
     `algorithm`, `statistic`, `tie_rules`, `calibrated`, `refusals`, and the echoed `S` and `N`.
     The two digests are rebuilt from the documented recipe by the module tests, and a rebuild
     here would replay that recipe with the same standard-library JSON and hash calls; `refusals`
     is the empty list in every result (docs/pbo.md, the result table), so it does not depend on
     the case and there is no value to recompute from one.
  2. Refusals provoked: `pbo_zero_variance` (code and trial ids, three cases), `pbo_odd_s`,
     `pbo_too_few_trials`, `pbo_sampler_unknown` (two cases: an unknown name, and `early_stop`,
     which is the study's own input and no sampler name), `pbo_status_error` for one error row and
     `pbo_block_sizes_uneven` (four deliberate layouts whose block sizes differ by two or more).
     Never provoked: `pbo_input_invalid` (a non-integer S, a bad count, sum or `m2`, mixed or
     repeated trial ids, S below 2, a bad `max_work`, a half with fewer than 2 intervals, a
     non-finite merged value, a malformed declared object or roster), `pbo_block_mismatch`,
     `pbo_resource_exceeded`, `pbo_parameter_missing`, the refusals of the parts
     (`pbo_roster_mismatch`, `pbo_return_blocks_mismatch`,
     `pbo_dataset_mismatch`, `pbo_window_mismatch`, `pbo_identity_mismatch`: every call is one
     part whose object and roster are those of its own rows, so no difference between parts or
     against the roster can arise), the row statuses other than an error row (`pbo_status_pruned`,
     `pbo_status_partial`, `pbo_non_positive_equity`), and the other ways a row can be incomplete
     (`pbo_rows_incomplete`: no status, an unknown status, no payload, a payload status other than
     `ok`).
  3. Random cases: S from 2 to 12, N from 2 to 8, blocks whose interval counts differ by at most
     one (default D2): a base of 2, 4 or 8 intervals and each block holding the base or one more,
     so the counts of a case are 2 and 3, 4 and 5, or 8 and 9, or all the base; integer returns
     between -40 and 40 (the sum of a block of 3, 5 or 9 intervals is drawn as a multiple of its
     count, so that its moments are exact in binary64), the `grid` roster and no early stop (the
     roster label is checked for each of the six samplers, without and with an early stop, in a
     section of its own: twelve rows). The run fails if no random case has counts that differ by
     exactly one, or if one has counts that differ by more. The layouts that D2 refuses are not
     drawn: four deliberate layouts have their spread checked in phase 1 and are refused with
     `pbo_block_sizes_uneven` in phase 2. Not reached: S above 12 (the enumeration section goes
     to S = 24 for the split count only), N above 8, blocks of one interval, counts other than 2,
     3, 4, 5, 8 and 9, non-integer returns, real data. In phase 2 the run also fails if no random
     case that the oracle computes (one it does not refuse for zero variance) has counts that
     differ, so that `equal_blocks: false` is compared at least once.
  4. A random case in which two trials tie exactly through different block moments, other than at a
     zero mean, is set aside, replaced and counted (its attempt number is printed): no case
     compares such a tie with the module. Ties that are compared with the module are those through
     identical block moments (the fixture `tie_s2_n3`, and the random cases, which copy whole
     trials and single blocks from earlier trials) and ties at a zero mean (kept in the random
     cases when they occur). The fixture `f4_atom_s2_n3` holds an atom at lambda 0, not a tie.
  5. Not independent evidence: the work figure is the pin's accounting formula replayed by loops,
     so agreement on it is by definition (it is anchored at the four pinned points); the lambdas
     use the platform's `math.log` on both sides; `equal_blocks`, `block_counts` and `coverage`
     are what the case declares (the interval count of every block, the number of trials), which
     the module is handed and reports back, so agreement shows that it reports the layout and
     the trial count it was given with the labels that docs/pbo.md defines, and is no reading of
     the data by another route.
  6. Selected-mode PBO and the native reducer are not in this tree.

### `pareto_oracle.py`

```
python3 tests/g2_oracles/pareto_oracle.py [--no-module] [--cases N] [--seed S]
```

* **Recomputes** the front, the domination counts, the distinct front vectors, the exclusions with
  their reasons and ids, and the admission figure.
* **Method.** The fixture files are read by the script itself (strict about NaN, Infinity and
  overflow), not through the module's reader. Dominance is judged three ways that must agree:
  signs of the per-axis comparisons turned towards each direction, the componentwise rule on
  oriented vectors, and a sort-and-filter pass for the front; counts come from every ordered
  pair.
* **Eligibility and refusals** are decided from the contract's tables, not by the module's ladder
  of tests. The kind of every axis cell of every row (absent, null, number, nonfinite, other) is
  computed first; a status table gives the reason of a row that is not ok; the exclusion
  families are sets (an ok row with an absent key, then an ok row with a null value); a status the
  table does not name is refused, so the eligibility accounting covers each row exactly once. The
  expected values of the eligibility hand cases
  (an absent key beats a null value, the axes of a metric reason are listed in the order of the
  request) and of the contract's refusal fixtures are written from the contract text, not taken
  from the module. The order of the refusals (unknown axis, then unusable value, then no
  eligible row) is the one the module documents; the pin does not state it, so that order is a
  reproduction, not an independent reading.
* **Fixtures.** `tests/python/fixtures/pareto/pf1.json`, `pf2.json`, `pf3.json`, the PF4
  reversal, the JSONL variants of PF1, eleven hand cases (ties, equal vectors, -0.0, adjacent
  doubles, `2**53`), three eligibility hand cases, the refusal fixtures of the contract, a
  hand-derived perturbation of PF1, and seeded random instances (`--cases`, default 300; ties,
  duplicates, absent and null values, and the statuses ok, pruned, constraint_violation,
  engine_error and partial: coverage limit 4 lists the statuses no case reaches), each also run
  with the rows shuffled and the axes reversed against the module.
* **Coverage limits.**
  1. Never compared: `input_sha256`, `interpretation`, `contract`, `algorithm`, and the echoed
     `axes`.
  2. Refusals provoked with their codes, ids and names compared: `pareto_axis_unknown`,
     `pareto_input_invalid` (a bad value on an ok row), `pareto_no_eligible_rows`. Provoked with
     the code only: a NaN token or an overflowing number in a file (`pareto_input_invalid`), a
     best-k result (`pareto_rows_incomplete`), axes omitted (`pareto_parameter_missing`), the
     direction `max` and a single axis (`pareto_parameter_invalid`), `max_work`
     (`pareto_resource_exceeded`). Never provoked: `pareto_context_mismatch`, a repeated axis
     name, an axis that is not an object or holds extra fields, an invalid `max_work` or
     `with_domination_counts`, and every refusal of the shared row reader (a repeated trial id or
     JSON key, an unknown status, `feasible` against the status, an id out of range,
     `trials_completed` or `warm_start` that disagree with the rows).
  3. The order in which the refusals are checked (unknown axis, unusable value, no eligible row)
     is the module's documented order and not the pin's: a reproduction.
  4. Random instances: 1 to 14 rows, 2 to 4 axes, metric values 0 to 3 (as int, float or -0.0),
     absent and null values, and rows with the statuses ok, pruned, constraint_violation,
     engine_error and partial; no non-integer value, and no instance large enough to say anything
     about scale. Not reached by any case: the statuses `constraint_error`, `trial_error` and
     `trial_timeout` appear in no fixture, hand case or random instance, and `objective_error`
     only in `pf3.json`, so a module error that is specific to one of those statuses would pass.
  5. Where the test pins only the front, the counts and the distinct count of a hand case are
     derived by hand in the script (each such literal is marked there).
  6. A non-finite number (NaN, an infinity or an overflowing integer) in an axis cell of a row that
     is not ok refuses in the oracle because the module refuses it (its documentation says so). The
     contract's wording names only ok rows and file tokens, so this is the module's reading
     reproduced, not an independent one, and no case exercises it.

### `neighbourhood_oracle.py` (covers `neighbourhood.py` and `sensitivity.py`)

```
python3 tests/g2_oracles/neighbourhood_oracle.py [--no-module] [--cases N] [--seed S]
```

* **Recomputes** the neighbour vectors per dimension kind, the class of each neighbour, the
  gaps, the near flags, counts, share near the best, coverage, dropped moves, the
  never-evaluated vectors in vector order, the pair ranking, the pair maps with their roles and
  diagonal totals, `top_ids`, the work figure and the refusals that the rows decide
  (conflicting duplicates, invalid centres, a relative band at a zero reference, the `max_work`
  boundary).
* **Method.** Domains are enumerated (integer lattices by ordinal, stepped reals by one
  rounding of the exact value `k*step + low` with the endpoint snap, booleans, typed
  categoricals); log integers by integer arithmetic on the ratio's exact numerator and
  denominator, cross-checked with Decimal; continuous and log reals in exact rationals (the
  oracle declines, rather than guesses, on a value that is not exactly binary64); pairs are
  ranked by repeated selection; the one-move sets of random small spaces are enumerated over
  the whole product.
* **Classification and aggregation** are not the module's code. The module indexes the rows by
  canonical JSON, takes the first scored row of a vector and accumulates counts, share and
  coverage cell by cell. The oracle files every row under its typed-tuple distance from the centre
  (the set of dimensions on which the row's vector differs from the centre's) and reads each cell
  off that partition; it names a cell's class from the contract's table (ok, constraint
  violation, any other status, no row), requires the scored rows of a vector to agree instead of
  trusting the first, shows an all-unscorable vector by its lowest-identity row, and recomputes
  the counts, the share near the best, the coverage and the diagonal totals from the finished
  cells, and the never-evaluated vectors as the vectors whose typed key is in no row (checked
  against the cells' classes). The gap and the band distance are one exact-rational step rounded
  once (the contract's one subtraction and one multiplication). Not independent, and labelled so:
  the near figure is read off the classification's own near flags (pinned by the worked fixtures
  NF1, NF3 and NF6 and by the module comparison), not from a second band test that would restate
  the classification's arithmetic; the 4-ulp acceptance of an off-lattice
  stepped real is the loader's rule restated in exact arithmetic (the pin names the loader, not
  the constant); and the validity rules for a row's values (an integer is an int on the lattice,
  a real an int or float inside the bounds, a boolean a bool, a categorical a declared choice of
  the same type with an int accepted for a float choice) are the loader's too, restated. They
  change no number on a valid row. The exact-rational arithmetic of the gap, the band distance
  and the ratios is another route to the same bits, not another result (coverage limit 6).
* **Calls.** Every call into `neighbourhood()` and `sensitivity` passes each argument the module
  requires, and none leans on a default the module no longer has. `pair_k` has no default in the
  module (`docs/neighbourhood.md`; pin N12), so every worked case, random space, random study and
  refusal case passes it, 0 where the case builds no pair map; an explicit 0 builds none and gives
  what the old default gave (`test_explicit_pair_k_zero_is_accepted_and_builds_no_map`). The
  oracle's own `analyse` takes `pair_k` as a required keyword, so a worked case or a random study
  that left it out would stop phase 1 with a `TypeError`, not be compared as if it had said 0.
  `fraction`, `ratio`, `max_work` and `space` are given where a case is about them and are
  otherwise left at the module's None. Not provoked by this script: a call without `pair_k` (a
  `TypeError`, pinned by `test_leaving_pair_k_out_is_an_error_of_the_call`) and `pair_k=None`
  (`neighbourhood_parameter_missing`, pinned by
  `test_absent_pair_k_is_refused_as_parameter_missing`).
* **Fixtures.** `tests/python/fixtures/neighbourhood/nf1_result.json`, `nf2_source0.json`,
  `nf2_source1.json`, `nf3_source0.json`, `nf3_source1.json`, `nf6_result.json` and
  `tests/python/fixtures/sensitivity/nf5_result.json`, `nf5b_result.json`, with their variants;
  the generation table (every row of `NF4Tests`); the whole nine-dimension case; the ranking and
  `not_near_counts` cases; NF7, derived by hand in the script (a pruned neighbour whose recorded
  objective is a prefix value, a vector held only by unscorable rows shown by its
  lowest-identity row, scored vectors with an unscorable duplicate of lower and of higher
  identity, and an ok row with a constraint violation on one vector in both id orders); seeded
  random small spaces (`--cases`, default 40) for the one-move sets; and as many seeded random
  studies (random rows with duplicates, the statuses ok, constraint_violation, engine_error,
  pruned, trial_timeout, objective_error and partial, pruned objectives, random band, direction
  and `pair_k`), where the module's whole document is compared with the oracle's. Also controls
  for `analyse` with a `set_aside` list, the route only the real-rows script takes (a small
  study derived by hand, with one declined dimension).
* **Coverage limits.**
  1. Never compared: `input_sha256`, `interpretation`, `contract`.
  2. Refusals provoked: `neighbourhood_duplicate_conflict` and `neighbourhood_centre_invalid`
     (code and rows named), `neighbourhood_band_degenerate` (code),
     `neighbourhood_resource_exceeded` (the `max_work` boundary). Never provoked: `input_invalid`,
     `rows_incomplete`, `space_missing`, `space_mismatch`, `context_mismatch`,
     `parameter_missing`, `parameter_invalid`, `no_varying_dimension`, `no_feasible_rows`.
  3. Reproduction, not independent: the 4-ulp acceptance of an off-lattice stepped real, and the
     validity rules for a row's value in each dimension kind (including an int accepted for a
     float choice), are the loader's rules restated (the pin names the loader, not the rules).
  4. Random studies: one source, 3 to 14 vectors held, up to four finite dimensions plus at most
     one continuous real, log real or log integer, the fraction 0.25 and the ratio 2.0 only, a
     short list of bands, no `max_work`. Not reached by random data: several sources and
     colliding ids (the fixtures NF1 read twice, NF2 and NF3 cover them), ratios that are not
     binary-exact (the sweeps cover log integer ratios 1.1 to 3.0), a categorical that mixes an
     int, a float and a boolean (the generation table covers it), a real value that is not
     exactly binary64 (the oracle declines instead of judging; on real rows that sets the centre
     and dimension aside, see the real-rows section), and rows with the statuses
     `constraint_error` and `trial_error`: the oracle's table classes them unscorable like the
     other statuses, but no fixture, hand case or random study holds one, so a module error that
     is specific to one of them would pass.
  5. The random small spaces of the one-move test hold a single row, so they test neighbour
     generation and never classification; the random studies test classification, and their
     per-centre class counts are recounted from the raw rows by a route other than the oracle's
     partition (`section_studies_internal`).
  6. Route only: the gap, the band distance and the share and coverage ratios are computed in
     exact rationals and rounded once, and for finite doubles one IEEE subtraction,
     multiplication or division gives the same bits. Agreement on them therefore checks the sign
     convention, the inclusive band test and the counts that feed the ratios, not the arithmetic.

## Real-rows end to end

Each script gates its input as native output (every row must carry a `backtest` object
counting bars; necessary, not sufficient), prints what the module produces, and compares it
with the same oracle.

### `pareto_real_rows.py`: the Pareto pin names `examples/single_strategy/study.json`

```
python3 tests/g2_oracles/pareto_real_rows.py --run \
    --engine-root external/pineforge-engine --native build/bin/pineforge-hpo-native \
    --out /scratch/pareto/pareto-result.json --cache-dir /scratch/pareto/cache \
    --axis metrics.all.net_profit=maximize --axis metrics.equity.max_equity_drawdown=minimize
python3 tests/g2_oracles/pareto_real_rows.py --result /path/to/pareto-result.json \
    --axis metrics.all.net_profit=maximize --axis metrics.equity.max_equity_drawdown=minimize
```

`--run` builds the native command line with `prepare_run` (the Python `run` wrapper does not
forward `--record-metric`), appends one `--record-metric` per axis, runs it and writes the result
to `--out`; `--out` and `--cache-dir` are both required with `--run` (see "What the scripts
write"). The result's `space_hash` must equal the golden digest of that study
(`docs/study-spec.md`) and all nine rows must be there, or the script exits 5. Directions are
required (pin P1: no default). A pass prints the front, a table of every row with the number of
rows that dominate it, the exclusions and the `interpretation`, then `RESULT: AGREE ...`, exit
0. Exit 1: module and oracle disagree; 2: the module refused (printed as `MODULE REFUSED:`; a
usage error also exits 2); 3: they agree on another
study (`--other-study` skips the digest and row-count gates, so the rows are not the study the
pin names; the line reads `RESULT: AGREE ON ANOTHER STUDY (exit 3)` and the run is never exit
0, because an agreement there is not the pin's proof); 5: gate failed; 7: the native run failed.

Coverage limits of `pareto_real_rows.py`:

1. Compared: the front, `n_front`, the distinct count, the domination counts, the rows read,
   eligible and excluded, and the exclusion summary; and the pin properties that the
   interpretation holds the items (a) to (g) in order, that the contract id is
   `pineforge-hpo-pareto-front/v1` and that the front is ascending.
2. Never compared: `input_sha256`, the echoed `axes`, `algorithm`; the interpretation text is
   checked only for its markers.
3. Only the nine rows of the golden study count as the pin's input; `--other-study` is
   exploratory and exits 3.
4. With `--result` the script cannot tell that the file came from `--run`; it checks the
   digest, the row count and the bar counters.

### `neighbourhood_real_rows.py`: the neighbourhood pin names no input

```
python3 tests/g2_oracles/neighbourhood_real_rows.py --result /path/to/result.json \
    --band absolute:5.0 --top-k 3 --pair-k 1
```

The pin names no input; the drafted proof plan says "a small real study (grid, a few parameters,
`--trials-file`)". The script therefore takes finished native result files (repeat `--result`
for more sources; any native result works, for example the `--out` file written by
`pareto_real_rows.py --run`) and invents none. `--band` and `--pair-k` have no default and are
required (`--pair-k 0` builds no pair map; leaving the option out is a usage error, exit 2, not
a run that quietly leaves the sensitivity half unexercised); add `--fraction` for a continuous
real dimension and `--ratio` for a log dimension. It prints the profile of every centre, the
never-evaluated vectors and the work figure, then compares the document with the oracle figure
by figure, and `top_ids`.

The oracle cannot always judge. A continuous or log real value whose moves (the value plus or
minus the fraction of the dimension's range, or times or divided by the ratio) are not exactly
binary64 numbers makes the oracle decline that dimension of that centre: it sets that (centre,
dimension) aside and does not guess. A set-aside makes only the comparisons it touches
inconclusive: the profile of that dimension, the `counts`, `dropped`, `share_near_best` and
`coverage` of that centre, its `pair_maps` when `--pair-k` asks for any (the pairs are ranked by
every dimension's count), and the document's `missing_vectors`, `lookups` and `work`. Everything
else, the other dimensions and the other centres included, is still compared. The verdict is
decided in this order:

1. Any disagreement outside the set-aside gives `RESULT: DISAGREE`, exit 1, whatever the
   set-aside count. A failed pin property, and rows that the oracle rejects although the module
   accepted them (for example a value that is not legal in the space), are disagreements too.
2. No disagreement but at least one set-aside gives `RESULT: INCONCLUSIVE`, exit 6, never an
   agreement.
3. No disagreement and no set-aside gives `RESULT: AGREE`, exit 0.

The count and the ids of the set-aside (centre, dimension) pairs are always printed: a
`SET ASIDE:` block before the comparisons, one `SET ASIDE` line for each comparison not made, and
the `RESULT:` line. Other exit codes: 2, the module refused (printed as `MODULE REFUSED:`; a
usage error also exits 2); 5, not native output. If the oracle declines the whole study (a
stepped real whose values it cannot enumerate) every comparison with the oracle is set aside as
one entry, with `--top-k` the module chooses the centres, and only the two properties the pin
states for the document are compared. Before it reads any input the script runs a self-test of
the three verdicts on a small study of the oracle that has one declined dimension.

Coverage limits of `neighbourhood_real_rows.py`:

1. Compared: `direction`, `reference`, `band`, `sources`, `duplicate_rows`,
   `constant_dimensions`, `missing_vectors`, the four fields of `admission` (`rows`, `lookups`,
   `work`, and `max_work` against the bound given with `--max-work`), and for every centre
   `source`, `trial_id`, `objective`, `centre_gap`, `centre_near`, `counts`, `dropped`,
   `share_near_best`, `coverage`, `pair_maps` and the `profile` (its dimension names, then each
   dimension); the module's `top_ids` against the oracle's; and the pin properties that the
   contract id is `pineforge-hpo-neighbourhood/v1` and that the centres come in ascending
   identity. The figures a set-aside touches are not compared (above).
2. Never compared: `rules`, `input_sha256`, `interpretation`.
3. A continuous or log real value whose moves are not exactly binary64 numbers is declined for
   its centre and dimension and set aside, not judged: the run can then end DISAGREE or
   INCONCLUSIVE, never AGREE. A fraction that is not itself a binary64 number, such as 0.1, makes
   the oracle decline most values of a continuous dimension (0.5 plus 0.1 needs more than 53
   bits); a fraction such as 0.25 usually does not (the random studies of
   `neighbourhood_oracle.py` use 0.25 and 2.0 for that reason).
4. Only native result files are accepted (recorded space, `trials_out` all, a bar counter in
   every row).

### `pbo_real_rows.py`: native blocks, original curves and PBO comparison

```
python3 tests/g2_oracles/pbo_real_rows.py --rows /path/to/result.json --S 16 --sampler grid \
    --dataset-identity TEXT --curves /path/to/curves.jsonl
```

The native runner writes `block_returns` and `return_blocks` when asked with `--pbo-blocks S`.
With rows that carry none the script prints `BLOCKED`, exits 3 and compares nothing; it never
builds rows. The PBO fixture files do carry `block_returns`, but not the `backtest`
object that marks native output, so the script refuses them (exit 5). With real rows it prints
the module's report as the pin describes it (`calibrated: false`, no threshold, both rates, the
roster label, the work figure) and compares the counts (by the count rule above) and the rates
with the exact oracle. With `--curves`, it first requires exact curve-to-moment comparisons
on every native row and block. The expected values come only from the original full-window
curves dumped by the separate proof executable. Equity bits, returns, exact Q/M and the
pairwise M identity use rational arithmetic. Explicit nearest-even rounding replays each
binary64 operation; counts, sum/m2 bits, both error bounds, K, n_b and boundary times must
match. Missing files, empty comparisons, duplicate ids and mismatched curve rosters fail.

The module takes its rows in parts, with the declared roster and the early stop of the study. The
script reads them from the result and fills in none of them: one part, the rows with the result's
`return_blocks` object plus the text of `--dataset-identity` (the caller states it; it is not a
key of the wire); the roster, the distinct trial ids of those rows; and `early_stop`, the
result's `early_stop` object or None. A rows list or a rows JSONL file is no result and declares
none of this, so the module refuses it (exit 2); a result without `return_blocks`, or a call
without `--dataset-identity`, is refused by the module in the same way.

Exact ties need care here, because the module ties on binary64 equality of its statistics
(default D4) and the oracle on exact equality. The script lists the ordered splits that a tie
through different block moments can touch (a tie among the training winners, or a testing
statistic equal to a winner's) under `TIES` (every touched split is named; the reasons are printed
for the first 20) and compares `n_splits` exactly. It asks the module for its lambdas, which carry
every ordered split (one lambda per tied winner, with the tie size), and compares the (tie size,
lambdas) of every split that no tie touches with the oracle's, exactly. It compares the touched
splits, the counts and the rates with what the ties allow (counts and rates against the range the
touched splits leave open), and it checks the module's counts against its own lambdas. A
difference at a touched split, or in the counts and rates inside that range, is `OPEN`:
inconclusive, never counted as an agreement. A difference at a split no tie touches, outside the
range, or between the module's counts and its own lambdas is a disagreement, and a disagreement is
reported (exit 1) whatever else is open; its `RESULT:` line restates the number of OPEN
comparisons. A tie therefore cannot hide a difference at any other split.

Exit codes: 0 agree, 1 disagree (or a property of the result that the pin states does not
hold), 2 a refusal (the module's, printed as `MODULE REFUSED:`, or the script's own for a
result that keeps only some of its trials, printed as `REFUSED BY THE SCRIPT:`; a usage error
also exits 2), 3 blocked, 4 oracle skipped as too large (`--oracle-limit`), 5 not native output,
6 inconclusive (nothing disagrees, but a comparison that exact ties touch differs inside the
range they allow).

Coverage limits of `pbo_real_rows.py`:

1. Compared: `n_splits`, the three counts (by the count rule) and both rates; the (tie size,
   lambdas) of every ordered split that no exact tie touches, exactly, and the module's counts
   against its own lambdas; and the pin properties that `calibrated` is false, that the result
   holds no standard error, threshold or p-value, that `pbo` equals `rate_le0`, that the counts
   add up to `n_splits` up to rounding, and that the roster label is the one of default D6.
2. Never compared: `work_estimate`, `input_sha256`, `interpretation`, `contract`, `algorithm`,
   and the lambdas and tie sizes of the splits that a tie touches (they are listed under
   `TIES`).
3. A run above `--oracle-limit` is not cross-checked (exit 4). A difference that exact ties can
   explain is `OPEN` and counts neither as an agreement nor as a disagreement. The tie analysis
   assumes that two distinct exact statistics never collide in binary64 (a near-tie); a
   collision of that kind would not be recognised as a tie and would show as a disagreement.
4. Blocks must be requested while producing native rows. Without that request the script exits
   3 (`BLOCKED`); fixtures without native bar counters stop at exit 5. Curve comparisons are
   required by the synthetic proof driver. PBO on real data is not proven.
5. The roster is the distinct trial ids of the rows, not an id set that the result declares (a
   result of this tree declares counters, such as `trials_completed`, and no id set), so the
   module's checks for a gap or an unknown trial in the lineage cannot fire here; a repeated id
   still refuses. The parts are one: the rows of the result (`warm_start_trials` first, then
   `trials`) under the `return_blocks` object of the result.
6. The check of the roster label expects the label of default D6 for the sampler and for the early
   stop together: whether the result had a stop is read from the result as the call reads it (an
   `early_stop` object or True is a stop, None or False is none; not from the module's output),
   and the expected label is the entry of `ROSTER_LABELS` in `pbo_oracle.py` for that pair, so a
   result with an `early_stop` object is expected to be `diagnostic_adaptive_roster` whatever the
   sampler. A sampler outside the six has no entry and the module refuses it first (exit 2). The
   `early_stop` flag that the module echoes is not compared by this script.

### Synthetic block/PBO mechanics route

This route proves mechanics only, not market behaviour. Its execution still needs validation.
It uses the 2,048 committed minute bars and 25-vector threshold grid in
`examples/pbo_proof/`. The integer-arithmetic generator records its seed and formulas.
The first 256 closes are flat below every entry level; the subsequent wave supplies
strategy activity. The controls select eligible blocks from the original curves.
The original single-strategy example and its golden digest are unchanged.

Build the separate test targets, then provide the threshold plugin produced by the ordinary
artifact compilation route. The plugin needs its adjacent `manifest.json`; its source hash
must match `examples/single_strategy/threshold.pine`, its plugin hash must match and its
compile flags must retain `-ffp-contract=off`. The plugin location below is an example:
substitute the path of the built artifact.

```bash
cmake --build build --target pineforge-hpo-native-proof pineforge_hpo_pbo_curve_reference
python3 -B tests/g2_oracles/pbo_native_proof.py \
    --native build/bin/pineforge-hpo-native-proof \
    --plugin build/cache/threshold/strategy.so \
    --harness build/bin/pineforge_hpo_pbo_curve_reference \
    --engine-root external/pineforge-engine --out build/pbo-proof --S 16
```

The driver uses the native producer function in `pareto_real_rows.py` with the synthetic
study and precompiled plugin. That function forwards `--pbo-blocks` and `--proof-curves`
after `prepare_run`, next to the metric flags. The original command is unchanged without
these options. Standalone alternate-study production uses `--study` with `--other-study`,
so it cannot be confused with the nine-trial Pareto example.

The driver independently admits the grid from the committed spec, requires every trial id
and parameter vector and checks that the original curves are distinct. Each execution writes
its own original full-window snapshot before releasing it; the fixed-candidate C-ABI probe
is a separate reconciliation and never supplies expected block moments.

The curve checker computes exact Q and M from rounded interval returns, checks the pairwise
M identity and requires exact counts and binary64 replay bits. It also requires
`abs(sum-Q) <= 8*n*u*A` and
`abs(m2-M) <= 16*n*u*M + 4*n*(u*A)^2`, where `u=2^-53` and `A=sum(abs(r))`.
These are sampled numerical error checks. The checker unit tests include the named
constant-series and tiny-spread regimes that require the second term.

Every mutation starts from a passing result copy and keeps the original curves:

| Mutation | Required observed check |
| --- | --- |
| Change one count with n_b fixed | module `pbo_block_mismatch`, for the changed trial |
| Change one sum by one ulp in a zero-return block | module input accepted, then `curve_sum_bound` |
| Change one declared block count, linked row counts and K together | module input accepted, then `curve_count` |
| Change one boundary time without decreasing the order | module input accepted, then `curve_boundary` |
| Force `non_positive_equity` | module `pbo_non_positive_equity`, for the changed trial |
| Change one finite m2 by one ulp in a block with count > 1 | module input accepted, then `curve_m2_bits` |
| Null one payload while other payloads remain | module `pbo_rows_incomplete`, for the changed trial |

For the one-ulp sum control, the original curve must supply a block with A=Q=0. Its bound
is zero, so the next binary64 value above the zero wire sum violates the exact bound.
The checker applies the same bound formula to every block; no block index is hard-coded.
A missing eligible block, acceptance, an unrelated refusal, a missing control or a bypassed
named check fails.

The last comparison runs `pbo_real_rows.py` on the unmodified rows with `--curves`. Only
exit 0 plus its actual `RESULT: AGREE` line passes; exits 1 through 6 all fail. The driver
prints the code and verdict it read. A fresh subdirectory retains original curves, native
rows, mutated copies, stdout/stderr logs and `metadata.json` with input and artifact
hashes, command arguments, the reducing build identity and observed verdicts.

CTest registers `pineforge_hpo_pbo_native_proof` whenever the native test targets are
available. Configure `PINEFORGE_HPO_PBO_PROOF_PLUGIN` with the built plugin path.
The fixture builds the proof executable and C-ABI probe; missing inputs fail with no skip.
`pineforge_hpo_pbo_curve_checker` runs the hand-derived Python checks without a native
binary. `pineforge_hpo_pbo_proof_cli` uses a separate invocation-count fixture to catch
rerun dumps, wrong equity bits and prefix curves, and checks production option isolation.
