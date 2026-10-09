# Sobol engine fixtures

Everything here supports `tests/test_sobol_engine.cpp` (UNEXECUTED when committed). The 64-bit
sequence has no published numbers above index 2^32 - 1, so the proof has two legs: the published
32-bit evidence below (an exact prefix relation) and an independent big-integer oracle.

## Published 32-bit evidence (copied unchanged)

The four TSV files are byte copies of the evidence of the `methods-sobol-contract` lane, produced
there by an independent Python oracle from the authors' notes and checked against the Joe-Kuo page
(`./sobol 10 3 new-joe-kuo-6.21201`) and the scipy `Sobol` documentation example. Their hashes are
in `SHA256SUMS`.

| File | Content | Used as |
|---|---|---|
| `vectors-gray-n0-31-d1-8.tsv` | first 32 Gray-order points, dimensions 1..8, 32-bit hex words | `sobol_coordinate64(n, c, any seed, None) == word << 32` |
| `vectors-powers-of-two-d1-6.tsv` | points at 2^k-1, 2^k, 2^k+1 (k = 1..31) and 0, 1, 2, 2^32-2, 2^32-1, dimensions 1..6 | same relation, up to the last 32-bit index |
| `direction-numbers.tsv` | V_1..V_32 for dimensions 1..8, 64, 1024 | `sobol_directions64(d - 1)[k - 1] == V_k << 32` |
| `digital-shift-vectors.tsv` | high 32 bits of the SplitMix64 shifts (six seeds, six columns) and shifted points | `sobol_shift64(seed, c) >> 32` and shifted-point high halves |

The relation `X64(n) = X32(n) << 32` for n < 2^32 holds because V_k(64) = V_k(32) << 32 for k <= 32,
and only k <= 32 occurs below 2^32. The shift files carry only the high halves: the low halves are
checked by the oracle.

## Independent oracle (generate on the spot box, do not check in)

`oracle64.py` reads only the vendored table and implements equations (2) and (4) of the authors'
notes with Python integers, SplitMix64 as a running generator, and first reproduces the files above.
It writes `D`, `P`, `S` and `Q` rows (direction numbers, points, shifts, shifted points) for 31
columns up to 1023, about 550 indices (0..70, 2^k-1, 2^k, 2^k+1 for k = 1..64, the last three
indices, 300 spread values) and six seeds. The test refuses a file whose row counts differ from its
header or whose table hash is not the pinned one.

```sh
python3 -I tests/fixtures/sobol/engine/oracle64.py \
    --table third_party/sobol_joe_kuo/new-joe-kuo-6.21201.first1024 \
    --fixtures tests/fixtures/sobol/engine --out build/sobol-engine/oracle64.tsv
build/bin/pineforge_hpo_test_sobol_engine --oracle build/sobol-engine/oracle64.tsv
```

The test binary fails without `--oracle`; `--no-oracle` runs the rest and prints that the 64-bit
proof is incomplete.
