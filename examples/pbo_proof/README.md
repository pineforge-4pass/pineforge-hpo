# Synthetic PBO mechanics input

This dataset proves mechanics only, not market behaviour. It provides reproducible input
for checking native block moments against original equity curves and then comparing the
PBO module with an independent exact oracle. PBO on real data is not proven.

The 2,048 minute bars start with 256 flat closes at 100, followed by a 96-bar triangle wave
with small deterministic integer offsets. The flat prefix supplies zero-return blocks for
the one-ulp sum-bound control; the remaining blocks contain strategy activity.
`generate.py` records the seed, the 32-bit recurrence and every price, volume and time formula.
It uses no external libraries. Regenerate from the repository root:

```bash
python3 examples/pbo_proof/generate.py
```

The output uses ASCII, LF line endings, two decimal places for prices and integer timestamps
in milliseconds. All prices are positive; each candle encloses its open and close.

The study reuses `../single_strategy/threshold.pine`. Its exhaustive grid has 25 vectors:
Entry Level is 101, 101.5, 102, 102.5 or 103, and Exit Level is 98, 98.5, 99, 99.5 or 100.
The sampler seed is 20260718. The original single-strategy example is unchanged.

Request 16 blocks for the proof. A curve with one point per bar has 2,047 intervals, with
block sizes 127 or 128; the smallest half contains 1,023 intervals. The proof checks the
actual reported layout and requires distinct curves for all 25 trials; these properties
remain subject to execution on the built strategy.
