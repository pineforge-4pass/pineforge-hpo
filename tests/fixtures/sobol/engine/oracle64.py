"""Independent big-integer oracle for the 64-bit Sobol engine (src/core/sobol_engine.cpp).

UNEXECUTED when committed: written on the Mac and first run on a spot box.

It reads only the vendored Joe-Kuo text table, never anything under src/, and implements the
authors' notes (Joe and Kuo, August 2008) with Python's arbitrary-precision integers:
  equation (2): m_k = 2 a_1 m_{k-1} ^ 2^2 a_2 m_{k-2} ^ ... ^ 2^(s-1) a_{s-1} m_{k-s+1}
                      ^ 2^s m_{k-s} ^ m_{k-s}      (k > s; a_1 is the top bit of a)
  equation (4): x_n = g_1 v_1 ^ g_2 v_2 ^ ..., g = n ^ floor(n / 2), v_k = m_k / 2^k
scaled by 2^64, plus SplitMix64 run as a stateful generator for the digital shift (the engine uses
the closed form seed + (column + 1) * gamma). Before writing anything it reproduces the published
32-bit evidence in this directory, so a wrong oracle cannot certify the engine.

    python3 -I oracle64.py --table <new-joe-kuo-6.21201.first1024> --fixtures <this directory> \
        --out <oracle64.tsv>

Output rows (tab separated, hex words are 16 digits):
    D column k V_k              direction number V_k of a column (column c is dimension c + 1)
    P column index X            unscrambled point
    S seed column shift         full 64-bit digital shift
    Q seed column index X'      shifted point
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

WORD = 64
MASK = (1 << WORD) - 1
GAMMA = 0x9E3779B97F4A7C15
TABLE_SHA256 = "52eeb57738dd69f5e1f593f2085c4efb3fb410c3af241d5396d7a2bef60b9257"
COLUMNS = list(range(16)) + [
    31, 32, 63, 64, 100, 127, 128, 255, 256, 511, 512, 767, 1000, 1022, 1023,
]
SHIFTED_COLUMNS = [0, 1, 2, 3, 7, 8, 31, 63, 64, 255, 512, 1023]
SEEDS = [0, 1, 42, 20260718, (1 << 63) + 5, (1 << 64) - 1]


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"oracle64: {message}")


def read_table(path: Path) -> tuple[dict[int, tuple[int, int, list[int]]], str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    check(digest == TABLE_SHA256, f"table SHA-256 {digest} is not the pinned subset")
    lines = raw.decode("ascii").split("\n")
    check(lines.pop() == "", "table lacks a final newline")
    check(lines[0].split() == ["d", "s", "a", "m_i"], "unexpected table header")
    table = {}
    for number, line in enumerate(lines[1:], start=2):
        fields = [int(field) for field in line.split()]
        check(fields[0] == number and len(fields) == 3 + fields[1], f"bad row {number}")
        table[number] = (fields[1], fields[2], fields[3:])
    check(len(table) == 1023, "table must hold dimensions 2..1024")
    return table, digest


def direction_integers(dimension: int, table: dict, count: int) -> list[int]:
    """m_1..m_count of Sobol dimension `dimension` (1-based), exact integers."""
    if dimension == 1:
        return [1] * count
    degree, polynomial, initial = table[dimension]
    m = list(initial)
    while len(m) < count:
        k = len(m) + 1
        value = (m[k - degree - 1] << degree) ^ m[k - degree - 1]
        for j in range(1, degree):
            if (polynomial >> (degree - 1 - j)) & 1:
                value ^= m[k - j - 1] << j
        m.append(value)
    return m[:count]


def directions(column: int, table: dict) -> list[int]:
    """V_1..V_64 of a column: m_k * 2^(64 - k), checked odd and below 2^k."""
    result = []
    for k, m in enumerate(direction_integers(column + 1, table, WORD), start=1):
        check(m % 2 == 1 and m < (1 << k), f"m_{k} of column {column} is not odd and below 2^{k}")
        result.append(m << (WORD - k))
    return result


def point(index: int, v: list[int]) -> int:
    check(0 <= index <= MASK, "index outside 0..2^64-1")
    gray = index ^ (index >> 1)
    x = 0
    for bit in range(WORD):
        if (gray >> bit) & 1:
            x ^= v[bit]
    return x


def splitmix_outputs(seed: int, count: int) -> list[int]:
    """The first `count` outputs of SplitMix64 started from `seed`, as a running generator."""
    state = seed & MASK
    outputs = []
    for _ in range(count):
        state = (state + GAMMA) & MASK
        z = state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
        outputs.append(z ^ (z >> 31))
    return outputs


def indices() -> list[int]:
    values = set(range(71))
    for bit in range(1, WORD + 1):
        for delta in (-1, 0, 1):
            value = (1 << bit) + delta
            if 0 <= value <= MASK:
                values.add(value)
    values.update({MASK - 2, MASK - 1, MASK})
    state = 0x123456789ABCDEF1
    for _ in range(300):  # a fixed xorshift64 stream, only to spread the sample
        state ^= (state << 13) & MASK
        state ^= state >> 7
        state ^= (state << 17) & MASK
        values.add(state)
    return sorted(values)


def fixture_rows(path: Path):
    for line in path.read_text(encoding="ascii").splitlines():
        if line and not line.startswith("#"):
            yield line.split("\t")


def self_check(directory: Path, table: dict) -> None:
    """Reproduce the published 32-bit evidence: the oracle must agree with it before use."""
    cache: dict[int, list[int]] = {}

    def column_directions(column: int) -> list[int]:
        if column not in cache:
            cache[column] = directions(column, table)
        return cache[column]

    for fields in fixture_rows(directory / "direction-numbers.tsv"):
        v = column_directions(int(fields[0]) - 1)
        for k, word in enumerate(fields[1].split()):
            check(v[k] == int(word, 16) << 32, f"V_{k + 1} of dimension {fields[0]} differs")
    for name, width in (("vectors-gray-n0-31-d1-8.tsv", 8), ("vectors-powers-of-two-d1-6.tsv", 6)):
        for fields in fixture_rows(directory / name):
            words = fields[1].split()
            check(len(words) == width, f"{name}: row width")
            for column, word in enumerate(words):
                got = point(int(fields[0]), column_directions(column))
                check(got == int(word, 16) << 32, f"{name}: n={fields[0]} column={column}")
    for fields in fixture_rows(directory / "digital-shift-vectors.tsv"):
        outputs = splitmix_outputs(int(fields[0]), 6)
        words = fields[-1].split()
        if len(fields) == 2:
            for column, word in enumerate(words):
                check(outputs[column] >> 32 == int(word, 16), f"shift seed={fields[0]} c={column}")
        else:
            for column, word in enumerate(words):
                got = point(int(fields[1]), column_directions(column)) ^ outputs[column]
                check(got >> 32 == int(word, 16), f"shifted point seed={fields[0]} n={fields[1]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--table", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args()

    table, digest = read_table(arguments.table)
    self_check(arguments.fixtures, table)

    d_rows, p_rows, s_rows, q_rows = [], [], [], []
    sample = indices()
    for column in COLUMNS:
        v = directions(column, table)
        for k, value in enumerate(v, start=1):
            d_rows.append(f"D\t{column}\t{k}\t{value:016x}")
        for index in sample:
            p_rows.append(f"P\t{column}\t{index}\t{point(index, v):016x}")
    for seed in SEEDS:
        shifts = splitmix_outputs(seed, 1024)
        for column, shift in enumerate(shifts):
            s_rows.append(f"S\t{seed}\t{column}\t{shift:016x}")
        for column in SHIFTED_COLUMNS:
            v = directions(column, table)
            for index in sample:
                q_rows.append(
                    f"Q\t{seed}\t{column}\t{index}\t{point(index, v) ^ shifts[column]:016x}"
                )

    lines = [
        "# oracle64 v1: independent big-integer reference for the 64-bit Sobol engine",
        f"# table_sha256={digest}",
        f"# counts D={len(d_rows)} P={len(p_rows)} S={len(s_rows)} Q={len(q_rows)}",
    ]
    lines += d_rows + p_rows + s_rows + q_rows
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_bytes(("\n".join(lines) + "\n").encode("ascii"))
    print(f"oracle64: wrote {arguments.out} (D={len(d_rows)} P={len(p_rows)} "
          f"S={len(s_rows)} Q={len(q_rows)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
