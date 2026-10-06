# Cross-vendor TPE reproducibility

## Arithmetic contract and checkpoint compatibility

Version 0.9.0's TPE algorithm revision is **2**. Supported environments are x86-64
with FMA3 and aarch64 Linux/macOS arm64, using IEEE-754 binary64, nearest rounding
and gradual underflow. The sampler checks rounding/FTZ/DAZ/FZ before initialization
and every `ask()`. Builds disable fast math, implicit contraction, numerical builtins
and LTO for these paths. `-fno-fast-math` precedes `-ffp-contract=off`, so Clang cannot
reset the latter while disabling fast math. Unfused-versus-fused contraction canaries
cover both the sampler C++ translation unit and the C library's exact kernel flags;
the C-kernel canary is an independent CTest and does not depend on MPFR availability.

The six vendored binary64 kernels are CORE-MATH `log`, `log1p`, `exp`, `expm1`, `cos`
and `erfc`, pinned at `aa66f20b0118453890acb29b98b51c9c8dd92118`. The sources and
accurate fallbacks are verbatim. Their MIT license/notices are retained under
`third_party/core_math/` and recorded in `NOTICE`. CORE-MATH provides correctly-rounded
implementations with function-specific error analyses and hard-case fallbacks; this
integration does not claim a new exhaustive formal proof for all six functions.
See the upstream project, FAQ and source error-bound comments.

`portable.h` prefixes kernel symbols and supplies deterministic explicit IEEE FMA,
hardware square root, integral rounding, adjacent-value stepping and power-of-two
scaling. Explicit FMA is intentional and required for error-free products; it is not
permission for compiler contraction. No host transcendental call is allowed on the
TPE proposal path. Non-transcendental classification/absolute-value operations only
inspect IEEE bits or execute exact sign operations.

`numeric_build_identity` contains the arithmetic contract and a cached self-probe of
vendored functions, not CPU vendor, libc, compiler brand or long-double precision.
The self-probe SHA-256 is
`7c4bfe58140a764dd4332895652e093ddb1e3eab849fe1ecf18c3b18e72b5b5b`.
An unavailable numeric-flags header still refuses restoration. The identity is not a
guarantee for arbitrary custom compiler modifications or altered vendored sources.

**v0.8.0 checkpoints rebuild ordered objective history.** Algorithm/identity mismatch
prevents importing old RNG/model/cache state; `warm_start_model` is `rebuilt_history`
and `warm_start_reason` names the numerical mismatch. History remains useful, but this
is not an uninterrupted revision-1 stream. A matching revision-2 checkpoint restores
only with the same seed, space, typed complete history, configuration and ask/tell
schedule. Objective evaluation is outside this portable-math contract: the application
must provide identical observations to obtain identical proposals.

### Application guidance

Runtime environment refusals become CLI **exit code 1** with the message text:
`portable TPE requires x86-64 FMA3`,
`portable TPE requires gradual underflow (FTZ/DAZ off)` on x86-64,
`portable TPE requires gradual underflow (FZ off)` on aarch64,
`portable TPE requires binary64 round-to-nearest evaluation`, or
`portable TPE refuses fast-math`. These are hard failures, not history rebuilds.
External plugins compiled with `-ffast-math`/`-Ofast` can enable FTZ/DAZ at load time,
so the sampler rechecks before every `ask()`. HPO's own artifact flags exclude fast math;
applications must ensure external plugins preserve the supported arithmetic environment.

The identity format changes from host `...libm_probe_sha256:` to
`portable-tpe-v2;...portable_probe_sha256:`. Treat the full identity as opaque and compare
for equality only. Its revision prefix and checkpoint signature derive from the same
`kTpeAlgorithmRevision`; a later revision bump therefore rebuilds with a mismatch reason.
The compiler-flags hash is intentionally not bound in this identity: portability binds
the semantic arithmetic contract, not arbitrary toolchain flags. Arbitrary numerical
compiler modifications remain unsupported; a missing generated flags header still
refuses restoration.

Configuration now requires a **C compiler as well as C++**. `PineForgeHPO::core`
transitively links `pineforge_hpo_portable_math`; manual link lines must include it.
The artifact cache has a one-time rebuild because `INPUT_METADATA_REVISION = 2`.
Before any trials, Python and native precompiled paths refuse `input_kind_schema: 1`
when the known recorded codegen version is below 1.1.0, and refuse disagreements with
adjacent provenance (marker, artifact key, plugin hash or codegen version).
Unknown-version markers remain trusted assertions, not inferred capabilities; external
builders must perform the symbol-input transpile canary and satisfy the
[documented provenance and input-kind guarantees](study-spec.md).

## Complete pre-change inventory

Locations below refer to base `50a995c` (v0.8.0), so the inventory remains meaningful
after code moves. Every listed transcendental/rounding call on TPE's proposal path is
now routed to portable binary64 implementations. TPE has no analytic inverse CDF:
its truncated-normal draw is Box–Muller plus rejection; CDF/log-tail evaluation is
used to normalize densities and integrate discrete bins.

| Base file:line | Function and operand type | Purpose |
|---|---|---|
| `src/core/tpe_sampler.cpp:85` | `fma`, double | Numerical identity's contraction canary |
| `src/core/tpe_sampler.cpp:208` | `log`, `sqrt`, double | Box–Muller radius from open uniform |
| `src/core/tpe_sampler.cpp:209` | `cos`, double | Box–Muller normal variate |
| `src/core/tpe_sampler.cpp:251` | `floor`, long double | Stepped-real cardinality |
| `src/core/tpe_sampler.cpp:258`, `:271` | `fma`, double | Decode grid ordinal without product cancellation |
| `src/core/tpe_sampler.cpp:260`, `:273` | `nextafter`, double | One-ULP endpoint allowance |
| `src/core/tpe_sampler.cpp:282` | `round`, long double | Encode stepped real |
| `src/core/tpe_sampler.cpp:305` | `floor`, long double | Discrete normalized coordinate to ordinal |
| `src/core/tpe_sampler.cpp:330` | Two `log` calls, double | Asymptotic normal log-CDF tail and correction |
| `src/core/tpe_sampler.cpp:333`, `:336` | `erfc`, double | Normal survival/CDF probabilities |
| `src/core/tpe_sampler.cpp:334` | `log1p`, double | Stable log-CDF near one |
| `src/core/tpe_sampler.cpp:336` | `log`, double | Negative normal log-CDF |
| `src/core/tpe_sampler.cpp:347` | `expm1`, `log`, double | Stable difference of log probabilities |
| `src/core/tpe_sampler.cpp:361`, `:362` | `log`, `log1p`, double | Narrow-bin integration and curvature correction |
| `src/core/tpe_sampler.cpp:435`, `:437` | `log`, double | Parzen component weights and bandwidths |
| `src/core/tpe_sampler.cpp:438` | `exp`, double | Normalized component amplitude |
| `src/core/tpe_sampler.cpp:445`, `:449` | `floor`, `ceil`, double | Density-table support bounds |
| `src/core/tpe_sampler.cpp:454`, `:455`, `:456` | `exp`, double | Density-table recurrence initialization |
| `src/core/tpe_sampler.cpp:495` | `log`, double | Interpolated density to acquisition log probability |
| `src/core/tpe_sampler.cpp:510`, `:555` | `log`, double | Narrow-bin width factor |
| `src/core/tpe_sampler.cpp:534` | `log`, `exp`, double | Pending-point bandwidth/amplitude |
| `src/core/tpe_sampler.cpp:542`, `:545`, `:547` | `exp`, `exp`, `log`, double | Pending density mixture |
| `src/core/tpe_sampler.cpp:556`, `:558`, `:560`, `:563` | `exp`, `exp`, `exp`, `log`, double | Pending bin-mass mixture |
| `src/core/tpe_sampler.cpp:598`, `:600` | `exp`, `log`, double | Stable log-sum-exp of mixture components |
| `src/core/tpe_sampler.cpp:672`, `:682` | `log`, double | Categorical and pending categorical likelihoods |
| `src/core/tpe_sampler.cpp:1530` | `ceil`, double | Gamma split's good-observation count |
| `src/core/tpe_sampler.cpp:1596`, `:1601` | `log1p`, two `log` calls, long double | Relative real log span, overflow fallback |
| `src/core/tpe_sampler.cpp:1607`, `:1625` | `log1p`, long double | Integer log span and normalized offset |
| `src/core/tpe_sampler.cpp:1683`, `:1694` | `expm1`, `floor`, long double | Decode log-integer bin |
| `src/core/tpe_sampler.cpp:1711`, `:1713` | `expm1`, `exp`, `log`, long double | Decode narrow/wide log-real span |
| `src/core/search_space.cpp:86` | `floor`, long double | Shared finite-real cardinality |
| `src/core/search_space.cpp:99`, `:112` | `fma`, double | Shared grid decoding |
| `src/core/search_space.cpp:101`, `:114`, `:158`, `:159` | `nextafter`, double | Endpoint allowance and distinct-grid validation |
| `src/core/search_space.cpp:248`, `:357` | `round`, long double | Finite-space encoding and proposal membership |

The additional libm-like calls are classifiers/sign operations, not approximations:
TPE `isfinite` at 250, 259, 357, 604–605, 706, 713, 1138, 1144, 1553, 1555,
1569, 1583, 1595, 1617–1618, 1626, 1634, 1684, 1716; double `abs` at 357,
1563, 1586. The 1617–1634/1684 checks had long-double operands. SearchSpace's
double classifiers are at 30, 100, 117, 161, 163, 309, 315, 335; long-double
classifiers at 80–81, 249, 247, 353–354; double `abs` at 157, 339 and long-double
`abs` at 166, 359–360. Objective/tell validation at TPE 2047/2125 is not proposal
math, but remains double. `std::min/max/clamp` only compare values.

`src/core/sampler.cpp` is **not called by TPE**. It is deliberately unchanged to
retain grid/random output: floor(long double) at 60/129; double FMA at 67/80 and
nextafter at 69/82; log1p(long double) at 117/142; expm1(long double) at 122;
log(long double) at 142 and exp(long double) at 143. In particular, legacy random
log-space transforms do use host libm; cross-vendor TPE portability does not silently
extend to RandomSampler's old numerical contract. Dlib/objective/pruner paths are also
outside this proposal call graph.

## Replacing extended precision and interval error analysis

Finite-grid subtraction uses error-free `TwoSum`; division retains the residual via
explicit IEEE FMA. An overflowing span is evaluated at half scale. Integer counts
are split into exact high/low binary64 parts rather than first rounding uint64 to
double. Floor/ordinal conversion applies the signed low residual before clipping,
including values approaching `UINT64_MAX`. No x86 80-bit or aarch64 128-bit arithmetic
participates. Tests cover decimal endpoints and ordinal residuals above 2^53 and
near 2^64. The existing 2^53 exactly-representable-bin restriction remains for TPE.

Log domains use binary64 `log1p((value-reference)/reference)` to retain narrow-span
information; overflowing ratios fall back to `log(value)-log(reference)`. `expm1`
retains small decode offsets and a log-domain `exp` fallback handles very wide real
spans. Endpoints/ordinals are clamped only to the legal search domain. Correct rounding
of a primitive does not imply correct rounding of the entire coordinate composition:
revision 2 defines a new binary64 proposal stream, tested on extreme/narrow domains,
rather than pretending to reproduce the old extended-precision stream.

To avoid unnecessary expensive CDF/log round trips, well-conditioned tail probabilities
are subtracted directly and logged once; cancellation-sensitive/tiny tails retain the
log-difference fallback. For interval width `w`, midpoint `m`, half-width `h`, the
central branch integrates `exp(-m*h*t-h*h*t*t/2)` for `t` in [-1,1]. Its coefficients
obey `c[n] = (-m*h*c[n-1]-h*h*c[n-2])/n`; odd terms integrate to zero. The correction
uses even coefficients through degree 24 and `log1p`. Entry requires
`w*max(1,abs(m)) < 0.25`, hence `q=abs(m*h)+h*h/2 <= 0.1328125`. Missing even terms
have degree at least 26 and exponential order at least 13, so their absolute integrated
tail is bounded by `exp(q)*q^13/13! < 8e-22`. Binary64 rounding error is separate:
27 MPFR-256 interval checks, including centers ±40, observe maximum scaled log error
`1.9508e-16` (test limit 64 binary64 epsilons). The pre-existing second-order branch
for widths below 1e-5 and asymptotic far-tail treatment remain explicitly approximate.

Mixture log-sum-exp omits terms more than 128 log units below its largest term.
Even an impossible 2^64-component mixture loses at most `2^64*exp(-128) < 4.8e-37`
relative mass. Bin integration first bounds a component by its maximum PDF on the
interval times the interval width and compares that bound with the known positive
prior's minimum PDF times the width, with an extra one-log-unit rounding margin.
Only similarly negligible tails avoid CDF evaluation. A fixed 64-entry immutable-model
bin-mass memo retains exact previously computed values; it neither consumes RNG bits
nor changes score arithmetic. Full-history models no longer compute unused density-table
amplitudes. These optimizations do not relax primitive accuracy or parity tolerances.
For intervals containing [-9, 9], the sum of the normal tails is below 2.3e-19,
less than half the binary64 spacing below one. The existing `1 - tails` therefore
rounds exactly to one and its logarithm to positive zero; returning that same zero
avoids two erfc evaluations without changing any proposal bits.

## Cross-vendor proof protocol

`tests/portable_math_proof.cpp` fixes seed 170905, generates **256 new trials per
space**, checkpoints after 128 and exports exact candidate bits, identity, history
and state. It seeds 4,200 deterministic observations so the mixed study actually
crosses the 4,096-history threaded-discovery threshold. Serial and eight-worker runs
must agree byte-for-byte; single-column spaces naturally have only one column to
schedule. Child runs import the Intel history/checkpoint and must both restore and
equal the remaining 128 uninterrupted proposals. The log-real space spans the smallest
normal positive double through 2^1023; mixed includes every type.

| Space | Proposal-stream SHA-256 | Linux Intel / AMD / aarch64 | macOS arm64 CI |
|---|---|---|---|
| Linear real | `23b846b458cbf212c45e19b0694e975a00a8c0a70f04e365313481c7e4ed3f19` | Match | Match |
| Log real | `9941da2c37a70a79b119931fed77bddd7efb8caec3ca800aa245d93c2ea06c0c` | Match | Match |
| Stepped real | `632b363541d0221bff48f63c2dc9d293e44f9e0d006d8f9e51d43223d5bd1004` | Match | Match |
| Linear int | `260fb38620390ba569a7936f61991249055e6624a5debc08112d1006b333af89` | Match | Match |
| Log int | `0b9c0fb78fda26bf85c63a9108d5af7da2981561f84f10d5b45e85a2610a00b2` | Match | Match |
| Categorical | `5e03af6d8bb5ba40f01c16a4b85cc61f1b5683444939d269c6c0bec138446761` | Match | Match |
| Bool | `f973ced11a4c71e747a7aefdf2bcd123784282b5ca3672a21378aa83ee1f7183` | Match | Match |
| Mixed | `c7360a846cd9d5c72c376b5bc7c4a62a8d4ce672ad17b9a732c956811694266c` | Match | Match |

All eight numerical identities have SHA-256
`acac13d55cab79e3d64d8d255c014d9278d55659d835819840013594bdb28613`.
The aggregate hashes include sorted space names, LF separators and exact proposal bytes.
Both serial and eight-worker modes produce this same result on all four hosts:

| Host | Aggregate proposal SHA-256 | Numeric identity SHA-256 |
|---|---|---|
| Intel x86-64 Linux cloud VM | `d118128454387b310c501ddb215df87731cdcb668ce7eb8aa46562c81ea6ba59` | `acac13d55cab79e3d64d8d255c014d9278d55659d835819840013594bdb28613` |
| AMD x86-64 Linux cloud VM | `d118128454387b310c501ddb215df87731cdcb668ce7eb8aa46562c81ea6ba59` | `acac13d55cab79e3d64d8d255c014d9278d55659d835819840013594bdb28613` |
| aarch64 Linux host | `d118128454387b310c501ddb215df87731cdcb668ce7eb8aa46562c81ea6ba59` | `acac13d55cab79e3d64d8d255c014d9278d55659d835819840013594bdb28613` |
| macOS arm64 CI runner | `d118128454387b310c501ddb215df87731cdcb668ce7eb8aa46562c81ea6ba59` | `acac13d55cab79e3d64d8d255c014d9278d55659d835819840013594bdb28613` |

The October 5, 2026 proof is reproducible from the committed Intel fixtures and hashes.
GitHub Actions run `37328176096` verifies the original reviewed head
`1bb896e2a6360a467115038592641334a935de79` (not subsequent review-fix commits):
Linux x86-64 job `111824159541` and macOS arm64 job `111824159809` publish proof artifacts.
The 24 Intel fixture files in `tests/fixtures/portable-math-intel/` were byte-checked
against the Intel artifacts. AMD, aarch64 Linux and macOS each imported all eight actual Intel
checkpoints, reported `restored_sampler_state` eight times and matched all 128 child
proposals per space against the uninterrupted Intel run. These are cross-host restore
proofs, not just same-host checkpoint tests. The fixture restore is also registered as
`pineforge_hpo_portable_math_intel_restore` in CTest, including sanitizer runs.

## Reproducible verification

`scripts/verify_portable_math.sh` supplies `release`, `proof`, `sanitizers`, `docs`,
`contract`, `optuna` and `performance` profiles. Linux release requires the regenerated
portable serial golden. The regenerated proposal-stream SHA-256 is
`3e093fefd5c00f9241a115ef5be729e42729bff41a30d3253485c43110451810`;
the fixture file SHA-256 is
`aa8070c4a24abb5c03e7bff25a7cba7488267064cb9d7dd657c533887b3f2971`.
The LD_PRELOAD differential retains all 22 function probes/eight spaces; replaced
functions must change neither proposal bits nor identity. MPFR is test-only and required
by both native CI jobs (`-DPINEFORGE_HPO_REQUIRE_MPFR=ON`); a missing header or library
fails configuration. Optional local builds explicitly print `SKIP: MPFR not found` and
return CTest skip code 77. CI prints the 104,962 exact binary64 primitive comparisons
and 27 interval checks with verbose test output.

Grid/random compatibility builds the same ten-space probe against v0.8.0 and 0.9.0
and compares raw candidate bits, plus the CLI contract's result bytes after changing
only the release-version field. Contract/E2E use the pinned canonical Pine harness.
The ten-space grid/random stream SHA-256 is
`5fc2113bfd59d669d570e21a27ceabbeba412f947784b6f9cc9797a7c9c57f47`
for both releases. The Linux A1 differential runs 176 perturbations (22 functions
times eight spaces), with unchanged proposal bits, numeric identities and warm restores.
The `asan` test preset includes bounded quarantine settings without disabling leak,
address or undefined-behavior checking; `ctest --preset asan` needs no extra shell flags.

Proposal performance uses four fresh independent builds per arm on a quiet c6i,
randomized build/arm order, one warmup and six runs per build. Every run fits 512
untimed trials and times only `ask()` for the next 256 in each of eight spaces.
Affinity is fixed; objective/tell time is excluded. The model is
`log(ns/proposal) = arm + build_random_intercept + repeat_error`, with method-of-moments
variance and conservative 95% Student-t intervals (3 degrees of freedom). Raw CSV,
binary hashes and the metadata sidecar are required, including failed experiments.
The initial literal-kernel replacement failed the 5% regression gate; interval
evaluation optimizations are measured separately, not disguised by a tolerance change.

## Proposal performance results

The final Intel cloud VM experiment uses four independent builds per arm,
with ccache disabled. Ratios compare revision 2 against v0.8.0; lower is faster.
Every individual space and the aggregate must have an upper 95% bound below 1.05.

| Space | Portable / v0.8.0 | 95% build-random-effect interval |
|---|---:|---|
| Linear real | 1.016715 | [0.994876, 1.039033] |
| Log real | 1.005209 | [0.984095, 1.026776] |
| Stepped real | 0.874447 | [0.824836, 0.927042] |
| Linear int | 0.860956 | [0.824190, 0.899362] |
| Log int | 0.840828 | [0.798501, 0.885398] |
| Categorical | 0.991938 | [0.986562, 0.997343] |
| Bool | 0.989295 | [0.986495, 0.992102] |
| Mixed | 0.835519 | [0.803053, 0.869298] |
| Aggregate | 0.891249 | [0.879831, 0.902814] |

The raw CSV and metadata sidecar are identified by the following SHA-256 checksums;
`scripts/verify_portable_math.sh performance` reproduces the measurement protocol.
CSV SHA-256: `23f0498761f5a93a582197c5cd70e255dd81dbb5065b25763ea78bc0ef1680bf`.
Metadata SHA-256: `0f1e50c3a7f77ee3c46d039be0c697b13a888fc556bc830432b2e56784c7bf86`.
The sidecar records all eight binary hashes, build order, CPU and variance components.
Earlier experiments remain retained: literal replacement ratio 1.5327
[1.5093, 1.5565], first interval optimization 1.3484 [1.3240, 1.3733], and the
first fast result 0.9196 aggregate but failing individual linear/log-real upper bounds
1.06855/1.05648. The final wide-interval fast path was required to pass every space.

## Native TPE versus Optuna

The pinned Optuna 4.9.0 smoke and standard six-problem/five-seed profiles both pass
on an aarch64 Linux host. The standard run uses seeds 17, 41, 73,
109 and 149 and each problem's declared trial budget. It emits 60 rows and checks
the declared optima in both implementations; all 1,000,000 discrete candidates are
enumerated, confirming a unique minimum of zero and a second-best value of 17.
The existing absolute optimum tolerance of 1e-12 and relative tie tolerance
`1e-12 * max(1, abs(native_regret), abs(optuna_regret))` are unchanged.

| Problem | Native median regret | Optuna median regret | Floor-adjusted median ratio | Wins native / Optuna / tie |
|---|---:|---:|---:|---|
| Branin-2 | 0.01112884 | 0.05180082 | 0.342 | 3 / 2 / 0 |
| Hartmann-3 | 0.003242738 | 0.008050801 | 0.734 | 3 / 2 / 0 |
| Rosenbrock-6 | 4.690910 | 5.828791 | 0.833 | 4 / 1 / 0 |
| Rotated Rastrigin-6 | 20.43591 | 17.13027 | 1.182 | 2 / 3 / 0 |
| Mixed log | 0.3561900 | 0.6045869 | 0.602 | 4 / 1 / 0 |
| Million discrete | 8711 | 10552 | 0.826 | 3 / 2 / 0 |

Aggregate paired wins are 19 / 11 / 0; the existing floor-adjusted, clipped
geometric paired regret ratio is 0.825. These are optimizer-quality observations,
not a promise of identical Optuna proposals or superiority on every problem.
Optuna found the exact million-discrete optimum in one of five runs; native TPE
found none. Smoke has 3 / 0 / 0 wins and a geometric ratio of 0.503; it is not the
full quality proof. Public-API timing includes Optuna's Python/storage overhead
and is distinct from the proposal-only regression experiment.

Standard CSV SHA-256: `76ac5bdf569ecefa34484d35ad5950f22f34b2e49fbd248e1fce017234264123`.
Metadata SHA-256: `ce2f09c8a5278b52408bfc187ccf8dbb373b741fc64fd0dea829d5d9c256ad7a`.
`scripts/verify_portable_math.sh optuna` reproduces the protocol.

## Release gate receipts

The numerical implementation at `deb9b1167df7ccab61a0484c0b87434f6b939a18` has
the following completed gates; subsequent release-documentation and line-wrapping
changes do not alter arithmetic or RNG consumption.

- AMD cloud VM and aarch64 Linux release: 40/40 CTests, required portable serial golden,
  85/85 Python tests.
- AMD cloud VM and aarch64 Linux contract: v0.8.0 `--baseline` plus canonical `--harness`, ten-space
  grid/random equality, and real-Pine input-kind/symbol-feed/metric/trade E2E equality.
- Linux ASan/UBSan: 39/39 CTests; `ctest --preset asan`
  uses only the checked-in quarantine environment (867.40 seconds).
- AMD cloud VM TSan: 39/39 CTests (671.64 seconds).
  The worker uses process-local `setarch -R`; no shared host sysctl is modified.
  Earlier sanitizer startup failures from incompatible randomized memory mappings
  are retained as failures, not called successful race checks.
- Zero-warning Doxygen and generated-site validation: 276 HTML files, AMD cloud VM,
  aarch64 Linux host and CI.
- Ruff 0.15.20: `check` and `format --check`, 26 Python files.
- GitHub Actions run `37328176096` (original reviewed head `1bb896e2a6360a467115038592641334a935de79`):
  native Linux x86-64, macOS arm64 and Python finish successfully. The same head also
  passes API docs, benchmark smoke and PR code-quality checks in their own workflows.

The committed Intel fixtures, published proof hashes and public GitHub Actions artifacts
support the cross-vendor claims. Use the verification profiles above to rerun the other
gates; results from the original reviewed head are not labeled as review-fix-head runs.
