#!/usr/bin/env python3
"""Independent reference for the Sobol mapper's floating-point cases (mapper revision 2).

STATUS: UNEXECUTED. Written under a rule that forbids running any generator on the Mac; the first
run belongs to the spot proof. Run it there as:

    python3 -I mapper_reference.py > float-reference.tsv

It uses only the Python standard library. Binary64 arithmetic (+, -, *, /) is Python's own IEEE
round-to-nearest-even float arithmetic, so the operation order below IS the contract. The
transcendental functions (log, log1p, exp, expm1) are evaluated with ``decimal`` at 900 digits and
rounded once to the nearest binary64, which is what a correctly rounded function returns. Nothing
here imports, reads or mimics the C++ sources beyond the formulas pinned in
methods-sobol-contract.revised.md N6 and the AR amendment of 2026-10-09 14:10.

Log-real rule (revision 2): z = L * u. If exp(z) is finite the result is low * exp(z). If exp(z)
overflows, q = exp(z * 0.25) and the result is ((((low * q) * q) * q) * q). Either way the value is
then clamped to [low, high]. L = log1p((high - low) / low) when that ratio is finite, otherwise
log(high) - log(low).

Output columns (tab separated, '#' lines are comments):
    kind  low  high  x_hex  expected  branch
kind is one of linear, logreal, logint. For linear and logreal, low, high and expected are the
0x-prefixed 16-digit hex bit patterns of binary64 values. For logint they are decimal int64 values.
branch is "literal" or "quartered" for logreal rows (which formula produced the value) and "-" for
every other row. For each log-real pair whose exp overflows somewhere, the script bisects the 53-bit
unit grid for the last finite exp and emits coordinates on both sides of that boundary, so the
fixture always contains witnesses for both branches.
"""
import math
import struct
import sys
from decimal import Decimal, getcontext

getcontext().prec = 900
MASK64 = (1 << 64) - 1
TWO_POW_MINUS_53 = 2.0 ** -53
UNIT_GRID_TOP = (1 << 53) - 1


def bits(value):
    return struct.unpack("<Q", struct.pack("<d", value))[0]


def hex64(value):
    return "0x%016x" % bits(value)


def to_double(decimal_value):
    # float(str) is correctly rounded to nearest-even; an out-of-range magnitude gives +-inf.
    return float(decimal_value)


def log_cr(value):
    return to_double(Decimal(value).ln())


def log1p_cr(value):
    return to_double((Decimal(1) + Decimal(value)).ln())


def exp_cr(value):
    return to_double(Decimal(value).exp())


def expm1_cr(value):
    return to_double(Decimal(value).exp() - Decimal(1))


def unit53(x):
    return float(x >> 11) * TWO_POW_MINUS_53


def clamp(value, low, high):
    return min(max(value, low), high)


def linear_real(low, high, unit):
    sampled = low + (high - low) * unit
    if not math.isfinite(sampled):
        sampled = low * (1.0 - unit) + high * unit
    if not math.isfinite(sampled):
        sampled = low
    return clamp(sampled, low, high)


def log_ratio_of(low, high):
    relative_span = (high - low) / low
    if math.isfinite(relative_span):
        return log1p_cr(relative_span)
    return log_cr(high) - log_cr(low)


def log_real(low, high, unit):
    """Returns (value, branch)."""
    log_ratio = log_ratio_of(low, high)
    exponent = log_ratio * unit
    growth = exp_cr(exponent)
    if math.isinf(growth):
        quarter = exp_cr(exponent * 0.25)
        v1 = low * quarter
        v2 = v1 * quarter
        v3 = v2 * quarter
        v4 = v3 * quarter
        return clamp(v4, low, high), "quartered"
    return clamp(low * growth, low, high), "literal"


def overflow_boundary(low, high):
    """Largest 53-bit unit-grid index k whose exp(L * k * 2^-53) is finite, or None if none overflows."""
    log_ratio = log_ratio_of(low, high)

    def finite(k):
        return math.isfinite(exp_cr(log_ratio * (float(k) * TWO_POW_MINUS_53)))

    if finite(UNIT_GRID_TOP):
        return None
    good, bad = 0, UNIT_GRID_TOP  # finite(0) is exp(0) = 1; finite(top) is False
    while bad - good > 1:
        middle = (good + bad) // 2
        if finite(middle):
            good = middle
        else:
            bad = middle
    return good


def log_integer(low, high, unit):
    span = high - low + 1
    assert 1 <= low <= 2 ** 52 and span <= 2 ** 53
    lower_edge = float(low) - 0.5
    span_d = float(span)
    log_ratio = log1p_cr(span_d / lower_edge)
    displacement = lower_edge * expm1_cr(log_ratio * unit)
    if displacement <= 0.0:
        return low
    if displacement >= span_d:
        return high
    return low + math.floor(displacement)


def splitmix64(state):
    state = (state + 0x9E3779B97F4A7C15) & MASK64
    z = state
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
    return state, z ^ (z >> 31)


def coordinates():
    fixed = [
        0x0000000000000000, 0x0000000000000003, 0x00000000000007FF, 0x0000000000000800,
        0x0000000000000C00, 0x00000001FFFFFFFF, 0x0000000100000000, 0x0000000180000000,
        0x3FFFFFFFFFFFFFFF, 0x5555555555555555, 0x7FFFFFFF80000000, 0x7FFFFFFFFFFFFFFF,
        0x8000000000000000, 0x8000000000000001, 0x8000000000000800, 0xAAAAAAAAAAAAAAAA,
        0xE000000000000000, 0xFC00000000000000,  # 63/64, the amendment's witness unit
        0xFFFFFFFF00000000, 0xFFFFFFFFFFFFF7FF, 0xFFFFFFFFFFFFF800,  # last: maximal 53-bit unit
        0xFFFFFFFFFFFFFFFF,
    ]
    state = 0x123456789ABCDEF
    drawn = []
    for _ in range(24):
        state, value = splitmix64(state)
        drawn.append(value)
    return fixed + drawn


LINEAR = [(0.0, 1.0), (-1.0, 1.0), (1e-5, 3.5), (-1e300, 1e300), (-1.7976931348623157e308, 1.7976931348623157e308),
          (1.0, 1.0000000000000002), (0.0, 9007199254740992.0), (-250.5, -3.25)]
SMALLEST_SUBNORMAL = 5e-324
SMALLEST_NORMAL = 2.2250738585072014e-308
LARGEST_FINITE = 1.7976931348623157e308
LOG_REAL = [(1.0, 100.0), (1e-5, 1e5), (0.5, 2.5), (1e-300, 1e300), (1e-300, 1e8),
            (SMALLEST_NORMAL, LARGEST_FINITE), (SMALLEST_SUBNORMAL, LARGEST_FINITE),
            (SMALLEST_SUBNORMAL, 1e300), (SMALLEST_NORMAL, 1e300), (SMALLEST_SUBNORMAL, 1.0)]
LOG_INTEGER = [(1, 2), (1, 4), (1, 1000000), (3, 1048576), (2 ** 52, 2 ** 52 + 2 ** 53 - 1), (1, 2 ** 53), (7, 7 + 99)]


def boundary_coordinates(low, high):
    k = overflow_boundary(low, high)
    if k is None:
        return []
    grid = [index for index in (k - 1, k, k + 1, k + 2) if 0 <= index <= UNIT_GRID_TOP]
    return [index << 11 for index in grid] + [(k << 11) | 0x7FF]


def main(out):
    out.write("# Generated by mapper_reference.py (mapper revision 2); see its docstring. Do not edit by hand.\n")
    out.write("# kind\tlow\thigh\tx_hex\texpected\tbranch\n")
    xs = coordinates()
    for low, high in LINEAR:
        for x in xs:
            expected = linear_real(low, high, unit53(x))
            out.write("linear\t%s\t%s\t0x%016x\t%s\t-\n" % (hex64(low), hex64(high), x, hex64(expected)))
    for low, high in LOG_REAL:
        for x in xs + boundary_coordinates(low, high):
            expected, branch = log_real(low, high, unit53(x))
            out.write("logreal\t%s\t%s\t0x%016x\t%s\t%s\n" % (hex64(low), hex64(high), x, hex64(expected), branch))
    for low, high in LOG_INTEGER:
        for x in xs:
            expected = log_integer(low, high, unit53(x))
            out.write("logint\t%d\t%d\t0x%016x\t%d\t-\n" % (low, high, x, expected))


if __name__ == "__main__":
    main(sys.stdout)
