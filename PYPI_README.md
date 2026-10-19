<img alt="Tunaxis" src="https://tunaxis.com/brand/v1/lockup-light.png" height="40">

# Tunaxis hyperparameter optimization

Native hyperparameter optimization for Tunaxis strategies.

formerly PineForge

This Python distribution provides StudySpec validation, PineScript artifact
initialization, and the `pineforge-hpo` command. Sampling, backtesting,
objective evaluation, and trial scheduling run in the separately built native
C++ executable.

The project is currently an alpha release. Its implemented executable path
optimizes one strategy over one OHLCV dataset; multiple-strategy shared-account
execution is not implemented yet. Get HPO from source or the release hub image
`ghcr.io/pineforge-4pass/tunaxis-release`
(the old `ghcr.io/pineforge-4pass/pineforge-release` image still works);
the HPO package is not published on PyPI.

- [Complete Quick Start](https://github.com/pineforge-4pass/pineforge-hpo#quick-start)
- [StudySpec reference](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/docs/study-spec.md)
- [C++ and Python API reference](https://pineforge-4pass.github.io/pineforge-hpo/)
- [Benchmark protocol](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/benchmarks/README.md)
- [License and dependency boundary](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/LEGAL.md)

The optional direct-PineScript bridge uses `pineforge-codegen` from the
`pineforge-codegen-oss` repository (`tunaxis-codegen` from the next codegen release).
The pinned source checkout installs `pineforge-codegen` and its `pineforge_codegen`
module. From the next release on, new imports use `tunaxis_codegen`;
`pineforge_codegen` remains available with a deprecation warning. The `transpile`
extra admits codegen >= 1.1.0 and < 2; install the release that matches your Tunaxis
engine (`pineforge-codegen==1.6.1` with engine v1.6.1 for the gitlinks).
A precompiled Tunaxis strategy plugin can be optimized without installing that
transpiler.

## License

Starting with v0.11.0, original code is source-available under the
[Tunaxis Source License 1.3](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/LICENSE).
The `LICENSE` file is the controlling text; the licensor is pineforge, LLC.
Noncommercial use and Personal Trading are free. Investment management (except
Personal Trading) and other Commercial Use require a commercial license from
<https://license.pineforge.dev>. One commercial license covers both codegen and HPO.
Releases up to and including v0.10.0 were released under Apache-2.0 and remain
available under that license. See the license and dependency boundary for
third-party notices.
