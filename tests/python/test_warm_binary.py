from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import io
import json
import math
from pathlib import Path
import struct
import unittest

from pineforge_hpo.continuation import WarmStartError, space_hash
from pineforge_hpo.study_spec import load_study_spec
from pineforge_hpo.warm_binary import HEADER, encode_warm_block, write_warm_block


FIXTURES = Path(__file__).parents[1] / "fixtures"


class WarmBinaryTests(unittest.TestCase):
    def setUp(self):
        self.study = load_study_spec(FIXTURES / "warm_v2_spec.json")
        self.trials = json.loads((FIXTURES / "warm_v2_trials.json").read_text())

    def test_golden_block(self):
        block = encode_warm_block(self.study, self.trials)
        golden = json.loads((FIXTURES / "warm_v2_golden.json").read_text())
        self.assertEqual(block, bytes.fromhex(golden["single_hex"]))
        self.assertEqual(hashlib.sha256(block).hexdigest(), golden["single_sha256"])
        self.assertEqual(space_hash(self.study), golden["space_hash"])
        self.assertEqual(len(block), 247)
        header = HEADER.unpack_from(block)
        self.assertEqual(header[:10], (b"PFHWARM\0", 2, 0, 100, 247, 3, 5, 1, 1, 0))
        self.assertEqual(struct.unpack_from("<Q", block, 100 + 16)[0], 9007199254740993)

    def test_golden_chunks(self):
        first = encode_warm_block(self.study, self.trials[:1])
        second = encode_warm_block(self.study, self.trials[1:])
        golden = json.loads((FIXTURES / "warm_v2_golden.json").read_text())
        self.assertEqual(first + second, bytes.fromhex(golden["multi_hex"]))
        stream = io.BytesIO()
        self.assertEqual(write_warm_block(stream, self.study, self.trials), 247)
        self.assertEqual(stream.getvalue(), encode_warm_block(self.study, self.trials))

    def test_order_and_minimal_fields(self):
        rich = copy.deepcopy(self.trials)
        for trial in rich:
            trial.update(metrics={"discard": 1234}, error="discard", notes="discard")
        self.assertEqual(encode_warm_block(self.study, rich[::-1]),
                         encode_warm_block(self.study, self.trials))
        self.assertNotIn(b"discard", encode_warm_block(self.study, rich))

    def test_constraint_order_is_canonical(self):
        forward = replace(self.study, objective=replace(
            self.study.objective, constraints=("z expression", "a expression")))
        backward = replace(self.study, objective=replace(
            self.study.objective, constraints=("a expression", "z expression")))
        first, second = copy.deepcopy(self.trials), copy.deepcopy(self.trials)
        for left, right in zip(first, second):
            left["constraint_values"] = [1.0, -0.0]
            right["constraint_values"] = [-0.0, 1.0]
        self.assertEqual(encode_warm_block(forward, first), encode_warm_block(backward, second))

    def test_reject_invalid_records(self):
        mutations = [
            lambda rows: rows[0].update(trial_id=1 << 64),
            lambda rows: rows[0].update(trial_id=True),
            lambda rows: rows[1].update(trial_id=rows[0]["trial_id"]),
            lambda rows: rows[0].update(status="pending"),
            lambda rows: rows[0].update(feasible=False),
            lambda rows: rows[0].update(objective=float("nan")),
            lambda rows: rows[0].update(constraint_values=[]),
            lambda rows: rows[0]["parameters"].update(e_continuous=float("inf")),
            lambda rows: rows[0]["parameters"].update(a_integer=-9),
            lambda rows: rows[0]["parameters"].update(b_stepped=math.nextafter(-0.75, 0.0)),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                rows = copy.deepcopy(self.trials)
                mutation(rows)
                with self.assertRaises(WarmStartError):
                    encode_warm_block(self.study, rows)
        with self.assertRaises(WarmStartError):
            encode_warm_block(self.study, [])


if __name__ == "__main__":
    unittest.main()
