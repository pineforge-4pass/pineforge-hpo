"""Pin the registered licensor entity in the public license documents."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ENTITY = "PINEFORGE LLC"
ENTITY_PATTERN = re.compile(r"\bpineforge[\s,]*llc\b", re.IGNORECASE)
OLD_ENTITY_PATTERN = re.compile(r"pineforge,\s*LLC", re.IGNORECASE)
ENTITY_COUNTS = (("LICENSE", 3), ("NOTICE", 1), ("README.md", 1), ("PYPI_README.md", 1))


class LicenseEntityTests(unittest.TestCase):
    def test_licensor_entity_is_exact(self):
        for filename, count in ENTITY_COUNTS:
            with self.subTest(file=filename):
                text = (ROOT / filename).read_text(encoding="utf-8")
                self.assertIn(EXPECTED_ENTITY, text)
                self.assertNotRegex(text, OLD_ENTITY_PATTERN)
                self.assertEqual(ENTITY_PATTERN.findall(text), [EXPECTED_ENTITY] * count)


if __name__ == "__main__":
    unittest.main()
