# Sobol mapper fixtures

All files here are UNEXECUTED source preparation (methods-sobol-mapper leaf, base `d2f83326`).
Nothing was compiled or run on the preparing machine.

| File | Origin | Read by |
|---|---|---|
| `high64-boundaries.tsv` | Hand-derived exact integer cases of `floor(x * count / 2^64)` (derivation in each row). | `tests/test_sobol_mapper.cpp` |
| `mapper_reference.py` | Independent generator for the floating cases: Python IEEE floats plus `decimal` (900 digits) rounded once for log, log1p, exp, expm1. No project code is imported. | run once on spot |
| `float-reference.tsv` | NOT YET PRESENT. Output of `python3 -I mapper_reference.py > float-reference.tsv`. | `tests/test_sobol_mapper.cpp` |

`test_sobol_mapper.cpp` fails with an explicit message while `float-reference.tsv` is missing or has
fewer than 800 data rows (the script emits 820). That is intended: the file must come from a spot run of the script, be
reviewed, and be committed by the integrator; it is never hand-edited. The test compares bit patterns,
so a one-ulp difference between the CORE-MATH wrappers and the decimal reference is a real finding
(report it, do not loosen the comparison).

The test needs the compile definition `PFH_SOBOL_MAPPER_FIXTURES` set to this directory.
