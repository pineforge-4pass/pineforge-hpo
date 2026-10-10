"""Generate or verify the Sobol direction-number table included by src/core/sobol_engine.cpp.

The output is a text transformation of the vendored Joe-Kuo table: its integers are re-laid-out as
C++ initializers and nothing is computed from them (the engine derives the direction numbers at run
time). The authors' licence notice is copied into the output header verbatim.

    python3 -I scripts/gen_sobol_table.py --write    # rewrite the .inc
    python3 -I scripts/gen_sobol_table.py --check    # verify pinned hashes and the .inc
    python3 -I scripts/gen_sobol_table.py --stdout   # print what --write would write

The .inc is src/core/sobol_table_joe_kuo_d6_1024.inc.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "third_party/sobol_joe_kuo/new-joe-kuo-6.21201.first1024"
LICENCE = ROOT / "third_party/sobol_joe_kuo/LICENSE"
SUMS = ROOT / "third_party/sobol_joe_kuo/SHA256SUMS"
ENGINE_HEADER = ROOT / "src/core/sobol_engine.hpp"
OUTPUT = ROOT / "src/core/sobol_table_joe_kuo_d6_1024.inc"

TABLE_NAME = "new-joe-kuo-6.21201"
TABLE_SHA256 = "52eeb57738dd69f5e1f593f2085c4efb3fb410c3af241d5396d7a2bef60b9257"
UPSTREAM_SHA256 = "68eedd2a4e3b659b9695e7aff0f8ac68718bcf620730fc3d3a8c65df2a067441"
LICENCE_SHA256 = "9d10226b50eeb34be0ab06bfa3392c7bd1f04bf602f9af4343295d1fd003d0e3"
TABLE_BYTES = 63469
TABLE_LINES = 1024
MAX_DEGREE = 13

HEADER = f"""\
// GENERATED FILE: do not edit. A text transformation of the vendored table, nothing more.
// Regenerate with: python3 -I scripts/gen_sobol_table.py --write
// Verify with:     python3 -I scripts/gen_sobol_table.py --check
//
// Input:  third_party/sobol_joe_kuo/new-joe-kuo-6.21201.first1024, the verbatim first 1024 lines
//         of new-joe-kuo-6.21201 by Stephen Joe and Frances Kuo (search criterion D(6)),
//         https://web.maths.unsw.edu.au/~fkuo/sobol/
// Input SHA-256:   {TABLE_SHA256}
// Upstream SHA-256: {UPSTREAM_SHA256}
// Licence SHA-256:  {LICENCE_SHA256}
//
// The authors' licence for the direction numbers follows verbatim
// (third_party/sobol_joe_kuo/LICENSE):
//
"""

FOOTER = """\
//
// Rows: dimension d = 2..1024, one per line, as {degree s, polynomial a, {m_1..m_s}} with the
// initial direction numbers zero padded to 13 entries. Dimension 1 has no row.
"""


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def fail(message: str) -> None:
    raise SystemExit(f"gen_sobol_table: {message}")


def read_verified(path: Path, digest: str, size: int | None = None) -> str:
    payload = path.read_bytes()
    if sha256(payload) != digest:
        fail(f"{path.relative_to(ROOT)} has SHA-256 {sha256(payload)}, expected {digest}")
    if size is not None and len(payload) != size:
        fail(f"{path.relative_to(ROOT)} has {len(payload)} bytes, expected {size}")
    return payload.decode("ascii")


def parse_table(text: str) -> list[tuple[int, int, list[int]]]:
    lines = text.split("\n")
    if lines.pop() != "":
        fail("the table does not end with a newline")
    if len(lines) != TABLE_LINES:
        fail(f"the table has {len(lines)} lines, expected {TABLE_LINES}")
    if lines[0].split() != ["d", "s", "a", "m_i"]:
        fail("unexpected table header row")
    rows = []
    for dimension, line in enumerate(lines[1:], start=2):
        fields = line.split()
        if not all(field.isascii() and field.isdecimal() for field in fields):
            fail(f"dimension {dimension}: non-decimal field")
        values = [int(field) for field in fields]
        if len(values) < 4 or values[0] != dimension:
            fail(f"dimension {dimension}: row out of order or too short")
        degree, polynomial, initial = values[1], values[2], values[3:]
        if not 1 <= degree <= MAX_DEGREE or len(initial) != degree:
            fail(f"dimension {dimension}: degree {degree} or width {len(initial)} out of range")
        if polynomial >= 1 << (degree - 1):
            fail(f"dimension {dimension}: polynomial {polynomial} exceeds {degree - 1} bits")
        for k, m in enumerate(initial, start=1):
            if m % 2 == 0 or m >= 1 << k:
                fail(f"dimension {dimension}: m_{k} = {m} is not odd and below 2^{k}")
        rows.append((degree, polynomial, initial))
    return rows


def render() -> str:
    table = read_verified(TABLE, TABLE_SHA256, TABLE_BYTES)
    licence = read_verified(LICENCE, LICENCE_SHA256)
    lines = [HEADER]
    # The notice is reproduced as `// ` comment lines; the licence has no trailing whitespace.
    for line in licence.split("\n")[:-1]:
        lines.append(("// " + line).rstrip() + "\n")
    lines.append(FOOTER)
    for degree, polynomial, initial in parse_table(table):
        padded = initial + [0] * (MAX_DEGREE - degree)
        lines.append("{%d, %d, {%s}},\n" % (degree, polynomial, ", ".join(map(str, padded))))
    return "".join(lines)


def check_constants() -> None:
    """The engine header, the sums file and this script must pin the same hashes."""
    header = ENGINE_HEADER.read_text(encoding="ascii")
    expected = {
        "kSobolTableSubsetSha256": TABLE_SHA256,
        "kSobolTableUpstreamSha256": UPSTREAM_SHA256,
        "kSobolLicenceSha256": LICENCE_SHA256,
    }
    for name, digest in expected.items():
        found = re.search(name + r'\[\]\s*=\s*"([0-9a-f]{64})"', header)
        if found is None or found.group(1) != digest:
            fail(f"{name} in {ENGINE_HEADER.relative_to(ROOT)} does not match this script")
    if f'kSobolTableName[] = "{TABLE_NAME}"' not in header:
        fail("kSobolTableName does not match this script")
    sums = {}
    for line in SUMS.read_text(encoding="ascii").splitlines():
        digest, name = line.split("  ", 1)
        sums[name] = digest
    if sums != {TABLE.name: TABLE_SHA256, LICENCE.name: LICENCE_SHA256}:
        fail(f"{SUMS.relative_to(ROOT)} does not list exactly the two pinned files")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="rewrite the generated table")
    mode.add_argument("--check", action="store_true", help="fail unless the table is current")
    mode.add_argument("--stdout", action="store_true", help="print the generated table")
    arguments = parser.parse_args()
    expected = render()
    if arguments.stdout:
        sys.stdout.write(expected)
    elif arguments.write:
        OUTPUT.write_bytes(expected.encode("ascii"))
    else:
        check_constants()
        if OUTPUT.read_bytes() != expected.encode("ascii"):
            fail(f"{OUTPUT.relative_to(ROOT)} is stale: rerun with --write")
        print(f"ok: {OUTPUT.relative_to(ROOT)} matches {TABLE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
