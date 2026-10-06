# Intel portable TPE checkpoint fixture

Written on an Intel Xeon cloud VM by the portable-math proof protocol, seed 170905,
4,200 deterministic seed observations, 256 proposed trials, and a checkpoint after 128.
The `.history` files contain the 128 newly proposed parent trials; the executable
reconstructs the deterministic seed observations. `.state` is the actual sampler
checkpoint (including its exact portable arithmetic identity), and `.proposals`
the uninterrupted 256-trial reference. These are test fixtures, not benchmark output.

Combined proposal SHA-256 (sorted space name, LF, exact proposal file bytes):
`d118128454387b310c501ddb215df87731cdcb668ce7eb8aa46562c81ea6ba59`.
Every identity SHA-256: `acac13d55cab79e3d64d8d255c014d9278d55659d835819840013594bdb28613`.

Run `pineforge_hpo_portable_math_proof OUT 8 tests/fixtures/portable-math-intel`.
It must print `restored_sampler_state` for all eight spaces and compare every child
proposal bit with the uninterrupted reference; rebuilding is a test failure.
