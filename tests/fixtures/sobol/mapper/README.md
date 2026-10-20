# Sobol mapper fixtures

The documented sealed 48-stage Sobol proof covers the handwritten integer fixtures and build-host
generation of the revision-2 floating reference followed by bitwise mapper checks on its recorded
builds, without establishing equality for unmeasured build pairs.
Source preparation: mapper revision 2, base `d2f83326`.
Nothing was compiled or run on the preparing machine.

| File | Origin | Read by |
|---|---|---|
| `high64-boundaries.tsv` | Hand-derived exact integer cases of `floor(x * count / 2^64)` (derivation in each row). | `tests/test_sobol_mapper.cpp` |
| `mapper_reference.py` | Independent generator for the floating cases: Python IEEE floats plus `decimal` (900 digits) rounded once for log, log1p, exp, expm1. No project code is imported. Implements the revision 2 log-real rule in the pinned operation order. | build-host reference generation |
| `float-reference.tsv` | Generated in the build tree by `python3 -I mapper_reference.py > float-reference.tsv`; not checked in here. | `tests/test_sobol_mapper.cpp` |

## Revision 2 (AR amendment, 2026-10-09 14:10)

When `exp(z)` (`z = L * u`) is finite the log-real value is the literal `low * exp(z)`, byte-identical to
revision 1. When it overflows, `q = exp(z * 0.25)` is computed once and the value is
`(((low * q) * q) * q) * q`, then clamped to `[low, high]`. The revision 1 half-exponent fallback is
retired because it can itself overflow (smallest subnormal .. `DBL_MAX` near unit 63/64).

The script's log-real ranges include the smallest subnormal and the smallest normal `low` paired with
`DBL_MAX`, `1e300` and `1.0`, plus ranges with a finite `high / low`. Its coordinates include unit
63/64 (`0xFC00000000000000`), the maximal 53-bit unit, the high-index anchors of columns 0 and 1, and,
for every range that overflows, five coordinates on both sides of the bisected last-finite-exp grid
point. Each log-real row carries a `literal` or `quartered` tag.

## Row count and failure policy

The script emits 1180 rows (22 fixed + 24 drawn coordinates for 8 linear, 10 log-real and 7
log-integer ranges, plus 5 boundary coordinates for each of the 6 log-real ranges that overflow).
`test_sobol_mapper.cpp` fails with an explicit message while `float-reference.tsv` is missing, has
fewer than 1100 rows, is not six-column, or lacks 60 witnesses of each branch. The build-host
reference generator writes the file in the build tree through tests/CMakeLists.txt. The output must
be reviewed; it is never hand-edited. The test compares bit patterns, so a one-ulp difference between
the CORE-MATH wrappers and the decimal reference is a real finding (report it; do not loosen the
comparison). A mismatch can be the oracle's fault, so investigate both sides.

tests/CMakeLists.txt sets the compile definition `PFH_SOBOL_MAPPER_FIXTURES` to a build-tree directory that holds a copy of `high64-boundaries.tsv` and the generated `float-reference.tsv`.
