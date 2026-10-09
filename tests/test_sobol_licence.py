#!/usr/bin/env python3
"""Licence and packaging proofs for the vendored Joe-Kuo Sobol direction numbers.
Usage: test_sobol_licence.py REPOSITORY_ROOT [unittest arguments].

UNEXECUTED until the proof phase. Static part (always): the vendored table and licence hash to the
pinned values, the licence copy in THIRD_PARTY_LICENSES is byte-identical, NOTICE carries the
attribution and the no-endorsement sentence, pyproject.toml lists the licence in license-files,
and no `sobol.cc` file is in the tree. Package part (only when built packages are supplied):

    python3 -m build --sdist --wheel --outdir DIR        # or: pip wheel --no-deps -w DIR .
    PFH_SOBOL_SDIST=DIR/pineforge_hpo-*.tar.gz PFH_SOBOL_WHEEL=DIR/pineforge_hpo-*.whl \\
        python3 tests/test_sobol_licence.py .

records exactly what the source distribution and the wheel contain. The wheel is the pure-Python
package: it carries the licence text in its metadata (the notice for the native components that
are distributed beside it) but not the table or any C++ source; the source distribution carries
the table, the generated table include, the engine sources and every licence file.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import tarfile
import tomllib
import unittest
import zipfile


ROOT = Path(sys.argv.pop(1)).resolve()
TABLE_SUBSET_SHA256 = "52eeb57738dd69f5e1f593f2085c4efb3fb410c3af241d5396d7a2bef60b9257"
TABLE_UPSTREAM_SHA256 = "68eedd2a4e3b659b9695e7aff0f8ac68718bcf620730fc3d3a8c65df2a067441"
LICENCE_SHA256 = "9d10226b50eeb34be0ab06bfa3392c7bd1f04bf602f9af4343295d1fd003d0e3"
TABLE = "third_party/sobol_joe_kuo/new-joe-kuo-6.21201.first1024"
LICENCE = "third_party/sobol_joe_kuo/LICENSE"
COPY = "THIRD_PARTY_LICENSES/sobol_joe_kuo.txt"
SDIST_REQUIRED = (
    "LICENSE", "NOTICE", COPY, LICENCE, TABLE, "third_party/sobol_joe_kuo/SHA256SUMS",
    "third_party/sobol_joe_kuo/README.md", "src/core/sobol_table_joe_kuo_d6_1024.inc",
    "src/core/sobol_engine.cpp", "src/core/sobol_engine.hpp", "scripts/gen_sobol_table.py",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StaticTests(unittest.TestCase):
    def test_vendored_bytes_hash_to_the_pinned_values(self) -> None:
        self.assertEqual(digest(ROOT / LICENCE), LICENCE_SHA256)
        self.assertEqual((ROOT / LICENCE).stat().st_size, 1821)
        self.assertEqual(digest(ROOT / TABLE), TABLE_SUBSET_SHA256)
        self.assertEqual((ROOT / TABLE).stat().st_size, 63469)
        table = (ROOT / TABLE).read_bytes()
        self.assertEqual(table.count(b"\n"), 1024)
        self.assertTrue(table.startswith(b"d       s       a       m_i"), table[:40])
        self.assertTrue(all(line.endswith(b" ") for line in table.split(b"\n")[:-1]),
                        "the upstream trailing space must be kept")
        sums = (ROOT / "third_party/sobol_joe_kuo/SHA256SUMS").read_text().split()
        self.assertEqual(sums[0::2], [TABLE_SUBSET_SHA256, LICENCE_SHA256])
        attributes = (ROOT / "third_party/sobol_joe_kuo/.gitattributes").read_text()
        self.assertIn("-text", attributes)
        self.assertIn(TABLE_UPSTREAM_SHA256,
                      (ROOT / "third_party/sobol_joe_kuo/README.md").read_text())

    def test_the_distribution_copy_is_byte_identical(self) -> None:
        self.assertEqual((ROOT / COPY).read_bytes(), (ROOT / LICENCE).read_bytes())
        self.assertEqual(digest(ROOT / COPY), LICENCE_SHA256)
        text = (ROOT / COPY).read_text()
        for phrase in ("Copyright (c) 2008, Frances Y. Kuo and Stephen Joe",
                       "Redistributions in binary form must reproduce",
                       "may be used to endorse or promote products"):
            self.assertIn(phrase, text)

    def test_notice_attributes_the_table(self) -> None:
        notice = (ROOT / "NOTICE").read_text()
        for phrase in ("Stephen Joe and Frances Kuo", "new-joe-kuo-6.21201", "dimensions 2 to 1024",
                       COPY, "are not used to endorse or promote this product",
                       "University of New South Wales", "University of Waikato"):
            self.assertIn(phrase, " ".join(notice.split()))
        # The product licence statements stay as they were.
        self.assertIn("PineForge\nSource License 1.2", notice)

    def test_pyproject_carries_the_licence_into_the_wheel_metadata(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual(project["license-files"], ["LICENSE", "NOTICE", COPY])
        self.assertEqual(project["license"], {"file": "LICENSE"})  # the product licence is unchanged

    def test_no_sobol_cc_in_the_tree(self) -> None:
        for path in ROOT.rglob("*"):
            relative = path.relative_to(ROOT).parts
            if relative and relative[0] in (".git", "external", "build"):
                continue
            self.assertNotEqual(path.name.lower(), "sobol.cc", str(path))

    def test_generated_include_carries_the_notice(self) -> None:
        include = (ROOT / "src/core/sobol_table_joe_kuo_d6_1024.inc").read_text()
        licence = (ROOT / LICENCE).read_text()
        commented = "\n".join(("// " + line).rstrip() for line in licence.splitlines())
        self.assertIn(commented, include)


class PackageTests(unittest.TestCase):
    sdist = os.environ.get("PFH_SOBOL_SDIST")
    wheel = os.environ.get("PFH_SOBOL_WHEEL")

    @unittest.skipUnless(sdist, "PFH_SOBOL_SDIST not set: no source distribution inspected")
    def test_source_distribution_contents(self) -> None:
        with tarfile.open(self.sdist) as archive:
            members = {}
            for member in archive.getmembers():
                if member.isfile():
                    members[member.name.split("/", 1)[1]] = archive.extractfile(member).read()
        for name in SDIST_REQUIRED:
            self.assertIn(name, members, f"the source distribution lacks {name}")
            self.assertEqual(members[name], (ROOT / name).read_bytes(), name)
        self.assertEqual(hashlib.sha256(members[TABLE]).hexdigest(), TABLE_SUBSET_SHA256)
        self.assertEqual(hashlib.sha256(members[COPY]).hexdigest(), LICENCE_SHA256)
        self.assertFalse([name for name in members if name.startswith("external/")])
        self.assertFalse([name for name in members if name.lower().endswith("sobol.cc")])

    @unittest.skipUnless(wheel, "PFH_SOBOL_WHEEL not set: no wheel inspected")
    def test_wheel_contents(self) -> None:
        with zipfile.ZipFile(self.wheel) as archive:
            names = archive.namelist()
            licences = [name for name in names if ".dist-info/licenses/" in name]
            copies = [name for name in licences if name.endswith("sobol_joe_kuo.txt")]
            self.assertEqual(len(copies), 1, licences)
            self.assertEqual(hashlib.sha256(archive.read(copies[0])).hexdigest(), LICENCE_SHA256)
            for required in ("LICENSE", "NOTICE"):
                self.assertTrue([name for name in licences if name.endswith("/" + required)],
                                f"the wheel metadata lacks {required}")
            metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
            self.assertEqual(len(metadata), 1)
            text = archive.read(metadata[0]).decode()
            self.assertIn("License-File: THIRD_PARTY_LICENSES/sobol_joe_kuo.txt", text)
            # The wheel is the Python package only: no table, no native source.
            self.assertFalse([n for n in names if n.endswith((".cpp", ".hpp", ".inc"))])
            self.assertFalse([n for n in names if "new-joe-kuo" in n])


if __name__ == "__main__":
    unittest.main()
