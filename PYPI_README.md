# Tunaxis hyperparameter optimization

Native hyperparameter optimization for Tunaxis strategies.

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

The optional direct-PineScript bridge uses the separately distributed
`tunaxis-codegen` package from the `pineforge-codegen-oss` repository
(previously `pineforge-codegen`). Use `tunaxis_codegen` for new
imports (`pineforge_codegen` still works with a deprecation warning). The
`transpile` extra admits codegen 1.1 to 1.3 (below 1.4); install the one whose version matches
your Tunaxis engine (for example `tunaxis-codegen==1.0.0` with engine v1.0.0).
A precompiled Tunaxis strategy plugin can be optimized without installing that
transpiler.

## License

Starting with v0.11.0, original code is source-available under the
[Tunaxis Source License 1.2](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/LICENSE).
The `LICENSE` file is the controlling text; the licensor is pineforge, LLC.
Noncommercial use and Personal Trading are free. Investment management (except
Personal Trading) and other Commercial Use require a commercial license from
<https://license.pineforge.dev>. One commercial license covers both codegen and HPO.
Releases up to and including v0.10.0 were released under Apache-2.0 and remain
available under that license. See the license and dependency boundary for
third-party notices.
