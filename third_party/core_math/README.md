# Pinned CORE-MATH binary64 kernels

Upstream: https://gitlab.inria.fr/core-math/core-math
Commit: `aa66f20b0118453890acb29b98b51c9c8dd92118`
License: MIT; original notices remain in every upstream source file.

The six kernels and their `dint.h` dependencies are copied verbatim. The upstream
README and license are retained as `UPSTREAM_README.md` and `UPSTREAM_LICENSE`.
`portable.h` is a PineForge integration shim, force-included only when compiling
these kernels. It prefixes public symbols and routes explicit FMA, integral
rounding and exponent scaling to fixed IEEE-754 operations, not host libm.
No polynomial, table, accurate fallback or error bound has been changed.

Explicit FMA is required by the kernels' error-free products; it is **not**
implicit contraction. Compilation disables contraction, fast math, builtins and
LTO. The supported hardware is x86-64 with FMA3 and aarch64, in round-to-nearest,
with gradual underflow. Unsupported runtime modes are refused before sampling.
