# Legal and dependency boundary

The original source code in this repository is licensed under the Apache
License, Version 2.0. See [LICENSE](LICENSE).

`pineforge-hpo` is designed to operate on compiled strategy plugins through the
public PineForge C ABI. That Apache-2.0 path does not require redistributing a
PineScript transpiler.

The optional direct-PineScript workflow may invoke the separately licensed
`pineforge_codegen` Python module through the pinned
`external/pineforge-codegen-oss` git submodule. It is not relicensed by this
repository, and users are responsible for complying with its terms. The
submodule need not be initialized when running an already compiled strategy
plugin. Git submodules preserve independent copyright and license boundaries;
their contents are not bundled into the Apache-2.0 HPO core.

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
