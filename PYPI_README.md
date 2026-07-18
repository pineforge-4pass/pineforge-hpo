# pineforge-hpo

Native hyperparameter optimization for PineForge strategies.

This Python distribution provides StudySpec validation, PineScript artifact
initialization, and the `pineforge-hpo` command. Sampling, backtesting,
objective evaluation, and trial scheduling run in the separately built native
C++ executable.

The project is currently an alpha release. Its implemented executable path
optimizes one strategy over one OHLCV dataset; multiple-strategy shared-account
execution is not implemented yet.

- [Complete Quick Start](https://github.com/pineforge-4pass/pineforge-hpo#quick-start)
- [StudySpec reference](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/docs/study-spec.md)
- [C++ and Python API reference](https://pineforge-4pass.github.io/pineforge-hpo/)
- [Benchmark protocol](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/benchmarks/README.md)
- [Apache-2.0 license and dependency boundary](https://github.com/pineforge-4pass/pineforge-hpo/blob/main/LEGAL.md)

The optional direct-PineScript bridge uses the separately distributed
`pineforge-codegen` package from the `pineforge-codegen-oss` repository. A
precompiled PineForge strategy plugin can be optimized without installing that
transpiler.
