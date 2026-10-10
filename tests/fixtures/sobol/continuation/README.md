# Sobol continuation fixtures

Static, hand-written inputs for `tests/test_sobol_continuation.cpp`. UNEXECUTED source preparation.
None of these files was produced by the code under test: the texts were typed by hand from the
documented canonical forms and hashed with `shasum -a 256`.

| File | Content | Hash source |
|---|---|---|
| `space-k8.recorded.json` | Recorded space of one integer parameter `k` in `[0, 7]`, objective `net_profit` (maximize), as `dump_json(recorded_space(...))` writes it. | n/a |
| `space-k8.canonical.txt` | The same space in the space-hash v1 canonical form (`canonical_space`). | n/a |
| `space-k8.space_hash.txt` | `shasum -a 256 space-k8.canonical.txt` | shasum |
| `identity-input-k8-none.txt` | Canonical identity input for scramble `none` (null seed, null numeric identity, mapper contract `pineforge_sobol_mapper_v1`, revision 2). | n/a |
| `identity-input-k8-none.sha256` | `shasum -a 256 identity-input-k8-none.txt` | shasum |
| `identity-input-k8-shift-max.txt` | The same for `digital_shift` with seed `18446744073709551615` (seed as a decimal string). | n/a |
| `identity-input-k8-shift-max.sha256` | `shasum -a 256 identity-input-k8-shift-max.txt` | shasum |
| `parent-k8-none-fresh.json` | A complete fresh Sobol result: IDs 0 to 7, unscrambled, column 0. The values of `k` are the published van der Corput prefix on an 8-point lattice: 0, 4, 6, 2, 3, 7, 5, 1. Its `space_hash` and `sobol.identity` are the two hashes above. | hand-derived rows |
| `parent-k8-none-forged-row.json` | The same file with the row of ID 3 carrying `k = 3` instead of 2 (a forged row). | `diff` shows one changed line |

The files have no trailing newline, so `shasum` of a file is the hash of exactly the text the test
compares. Hashes at the time of writing: space hash `f7a2028a...1c98`, identity (none) `20c84f06...b499`,
identity (shift, seed 2^64-1) `a193d7ce...697d`; the full values are in the `.txt` and `.sha256` files.

The identity input contains `mapper_revision` 2 (AR amendment of 2026-10-09 14:10). If the mapper
revision or any listed field changes, regenerate with `printf` and `shasum` exactly as the test header
describes; never by running the code under test.
