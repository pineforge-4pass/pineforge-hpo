# Legal and dependency boundary

Starting with v0.11.0, the original source code in this repository is licensed
under the PineForge Source License 1.2. See [LICENSE](LICENSE) for the controlling
text. Releases up to and including v0.10.0 were released under Apache-2.0 and
remain available under that license. For commercial licensing of HPO and codegen,
see <https://license.pineforge.dev>.

`pineforge-hpo` is designed to operate on compiled strategy plugins through the
public PineForge C ABI. That compiled-plugin path does not require redistributing a
PineScript transpiler.

The optional direct-PineScript workflow may invoke the separately licensed
`pineforge_codegen` Python module through the pinned
`external/pineforge-codegen-oss` git submodule. It is not relicensed by this
repository, and users are responsible for complying with its terms. The
submodule need not be initialized when running an already compiled strategy
plugin. Git submodules preserve independent copyright and license boundaries;
their contents are not bundled into the HPO core.

The native optimizer integrates dlib's global function search. CMake downloads
the pinned dlib v20.0.1 source archive from its official repository by default;
a build may explicitly select an exact-version system package instead. dlib is
distributed under the Boost Software License 1.0, which permits use and
redistribution subject to retaining its copyright and permission notice. The
upstream project and full license text are available at
<https://github.com/davisking/dlib> and
[THIRD_PARTY_LICENSES/dlib.txt](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/THIRD_PARTY_LICENSES/dlib.txt).

Pine Script is a trademark of TradingView, Inc. PineForge is not affiliated
with, endorsed by, or sponsored by TradingView.

The TPE sampler includes the MIT-licensed CORE-MATH binary64 kernels for `log`,
`log1p`, `exp`, `expm1`, `cos` and `erfc`. Their upstream source revision, original
author notices and full license are retained in `third_party/core_math/`; see
`NOTICE` and [the integration record](docs/portable-math.md). MPFR is an
optional accuracy-test dependency only and is not linked into the runtime library.

The Sobol sampler uses the direction numbers of Stephen Joe and Frances Kuo
(file `new-joe-kuo-6.21201`, search criterion D(6)). Its first 1024 lines
(dimensions 2 to 1024) are vendored unchanged in `third_party/sobol_joe_kuo/`
and compiled into a generated table; the upstream source is
<https://web.maths.unsw.edu.au/~fkuo/sobol/>. The authors publish the table
under their BSD-style licence, Copyright (c) 2008, Frances Y. Kuo and Stephen
Joe. That licence text is retained verbatim in
`third_party/sobol_joe_kuo/LICENSE`, and
[THIRD_PARTY_LICENSES/sobol_joe_kuo.txt](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/THIRD_PARTY_LICENSES/sobol_joe_kuo.txt)
is an unchanged copy of it that accompanies source and binary distributions
(`NOTICE` carries the attribution; the wheel metadata carries the licence file).
The names of the copyright holders and of the University of New South Wales and
the University of Waikato are not used to endorse or promote this product. The
authors' example program `sobol.cc` is not used. The licence of the table does
not change the PineForge Source License 1.2 that governs the original source
code of this repository.
