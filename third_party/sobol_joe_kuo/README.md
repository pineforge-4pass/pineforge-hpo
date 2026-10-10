# Joe-Kuo Sobol direction numbers (vendored data only)

Direction numbers for the Sobol sampler: the first 1024 lines of the authors' file
`new-joe-kuo-6.21201` (Stephen Joe and Frances Kuo, search criterion D(6), "up to dimension
21201"), which are the header line and the rows for dimensions d = 2..1024. Dimension 1 has no row;
every m_k is 1.

| File | What it is | SHA-256 |
|---|---|---|
| `new-joe-kuo-6.21201.first1024` | verbatim lines 1-1024 of the upstream file (63,469 bytes, 1,023 rows) | `52eeb57738dd69f5e1f593f2085c4efb3fb410c3af241d5396d7a2bef60b9257` |
| `LICENSE` | verbatim upstream `licence` file (1,821 bytes) | `9d10226b50eeb34be0ab06bfa3392c7bd1f04bf602f9af4343295d1fd003d0e3` |
| (not vendored) | the complete upstream file `new-joe-kuo-6.21201` (1,887,612 bytes, 21,201 lines) | `68eedd2a4e3b659b9695e7aff0f8ac68718bcf620730fc3d3a8c65df2a067441` |

Both vendored files are byte-for-byte upstream copies. `.gitattributes` turns off text
normalisation, and the table's trailing space on every line is kept on purpose. Check them with
`cd third_party/sobol_joe_kuo && shasum -a 256 -c SHA256SUMS`, and re-derive the table subset
from a fresh download with `head -n 1024 new-joe-kuo-6.21201 | shasum -a 256`.

## Where the grant lives

The table file has no header, so the grant is the sentence on the authors' page
<https://web.maths.unsw.edu.au/~fkuo/sobol/> (HTTP `Last-Modified` Tue, 28 Sep 2010 05:29:19 GMT;
page bytes 13,320, SHA-256 `e52cd5a61238193d53093192560ece3a8ee4ad5d569947c4327ea044326f8baa`),
section "Simple C++ program":

> This program and the accompanying direction numbers above are covered by this BSD-style licence.

where "licence" links to `https://web.maths.unsw.edu.au/~fkuo/sobol/licence`, the file kept here as
`LICENSE` ("Licence pertaining to sobol.cc and the accompanying sets of direction numbers",
"Copyright (c) 2008, Frances Y. Kuo and Stephen Joe"). The same page puts the table in the
section "NEW sets of direction numbers from [2]" and says "Property A is satisfied up to dimension
1111".

Upstream file facts: `new-joe-kuo-6.21201` was fetched 2026-10-09 from
`https://web.maths.unsw.edu.au/~fkuo/sobol/new-joe-kuo-6.21201` (HTTP `Last-Modified` Thu, 16 Sep
2010 13:16:56 GMT, ETag `"4c9218c8-1ccd7c"`); `licence` has `Last-Modified` Fri, 23 May 2008
06:47:02 GMT. A Wayback capture of the page:
<http://web.archive.org/web/20230209233426/https://web.maths.unsw.edu.au/~fkuo/sobol/>.
The retained download directories and response headers are in the lane evidence store
`methods-sobol-contract.work` (not part of this repository).

## Obligations kept

The licence is the three-clause BSD text. Source redistributions keep the notice (`LICENSE` here and
the verbatim comment at the top of `src/core/sobol_table_joe_kuo_d6_1024.inc`); binary
redistributions reproduce it in documentation or other materials (the release integrator adds it to
`NOTICE`, `THIRD_PARTY_LICENSES/` and the wheel metadata); the authors' and universities' names are
not used to endorse this product. It does not change the PineForge licence of the rest of the tree.

## What is deliberately absent

- `sobol.cc`, the authors' example program, is not vendored, copied or translated. It carries
  separate requests (acknowledgement, e-mail notice, feedback) phrased for that source code and
  not for the table; the engine in `src/core/sobol_engine.cpp` is written from the recurrence in the
  authors' notes (Joe and Kuo, August 2008, equations (2) and (4)) alone. No e-mail is sent.
- The remaining 20,177 rows. More rows would be a prefix extension of this same table.

## Citation

The page asks that publications using the material cite S. Joe and F. Y. Kuo, "Remark on
Algorithm 659: Implementing Sobol's quasirandom sequence generator", ACM Trans. Math. Softw. 29
(2003), 49-57, and/or "Constructing Sobol sequences with better two-dimensional projections", SIAM
J. Sci. Comput. 30 (2008), 2635-2654, as appropriate. The request is not binding.
