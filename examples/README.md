# Examples

The examples are intentionally small and deterministic. They demonstrate the
two public entry points without requiring benchmark datasets.

## Direct PineScript study

[`single_strategy/`](single_strategy/) contains a PineScript strategy, a tiny
synthetic one-minute OHLCV fixture covered by this repository's Apache-2.0
license, and a complete StudySpec. It exercises the normal command-line path:

```bash
pineforge-hpo run examples/single_strategy/study.json \
  --engine-root external/pineforge-engine \
  --native ./build/bin/pineforge-hpo-native
```

The first run transpiles and compiles the strategy. Later runs reuse the
content-addressed artifact when the Pine source and toolchain identity have not
changed.

## Native ask/tell API

[`cpp/ask_tell.cpp`](cpp/ask_tell.cpp) constructs a typed finite search space
and drives the native TPE sampler directly:

```bash
cmake -S . -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DPINEFORGE_ENGINE_ROOT="$PWD/external/pineforge-engine" \
  -DPINEFORGE_HPO_BUILD_EXAMPLES=ON
cmake --build build --target pineforge_hpo_example_ask_tell -j4
./build/bin/pineforge_hpo_example_ask_tell
```

This example uses a synthetic objective so it does not need a strategy plugin
or market data. A real application evaluates each returned candidate, then
calls `tell()` with the resulting account or strategy objective.
