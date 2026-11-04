## Summary

Describe the user-visible change and why it belongs in `pineforge-hpo`.

## Compatibility and reproducibility

- StudySpec/API/result-schema impact:
- Seed or deterministic replay impact:
- Engine/codegen compatibility impact:

## Verification

- [ ] Checks touching the changed code were run; list the targeted commands and results.
- [ ] Each test added or changed covers one specific target, and a new test was added only
      when no existing test covers that target.
- [ ] Documentation and examples match the implementation.
- [ ] Benchmark claims include raw results, environment metadata, seeds, and equal budgets.
- [ ] No generated build output, private strategy source, market data, or secrets are included.

The final CI workflows run the repository's normal build and test suite.
