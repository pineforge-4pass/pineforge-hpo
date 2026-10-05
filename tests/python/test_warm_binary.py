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

from pineforge_hpo.continuation import (
    WarmStartError,
    _validate_sampler_checkpoint,
    space_hash,
)
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

    def test_optional_sampler_state(self):
        payload = "opaque checkpoint\n"
        state = (
            "PFHTPE2\n" + hashlib.sha256(payload.encode()).hexdigest() + "\n" + payload
        )
        encoded = state.encode()
        plain = encode_warm_block(self.study, self.trials)
        expected = plain + b"PFHSTATE" + struct.pack("<Q", len(encoded)) + encoded
        self.assertEqual(
            encode_warm_block(self.study, self.trials, sampler_state=state), expected
        )
        stream = io.BytesIO()
        self.assertEqual(
            write_warm_block(stream, self.study, self.trials, sampler_state=state),
            len(expected),
        )
        self.assertEqual(stream.getvalue(), expected)
        for invalid in ("", 1, "x" * (16 * 1024 * 1024 + 1)):
            with self.assertRaises(WarmStartError):
                encode_warm_block(self.study, self.trials, sampler_state=invalid)

    def test_symbol_feeds_header_and_inference(self):
        record = {
            "canonicalization": "pf-symbol-feed-barc-close-le-v1",
            "symbols": {
                "BINANCE:ETHUSDT": {
                    "facts": {"mintick": 0.01},
                    "feeds": {
                        "240": {
                            "bars": 2,
                            "first_ts": 1700000000000,
                            "last_ts": 1700014400000,
                            "source_values_sha256": "a" * 64,
                        }
                    },
                }
            },
        }
        study = replace(
            self.study,
            symbol_feeds={
                "symbols": {"BINANCE:ETHUSDT": {"feeds": {"240": "eth.csv"}}}
            },
        )
        encoded_record = json.dumps(
            record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        plain = encode_warm_block(self.study, self.trials)
        block = encode_warm_block(study, self.trials, symbol_feeds_record=record)
        header = HEADER.unpack_from(block)
        self.assertEqual(header[2], 1)
        self.assertEqual(header[3], 100 + 4 + len(encoded_record))
        self.assertEqual(header[4], len(block))
        self.assertEqual(struct.unpack_from("<I", block, 100)[0], len(encoded_record))
        self.assertEqual(block[104 : header[3]], encoded_record)
        self.assertEqual(block[header[3] :], plain[100:])
        rows = copy.deepcopy(self.trials)
        for row in rows:
            row["space"] = {"symbol_feeds": record}
        self.assertEqual(encode_warm_block(study, rows), block)
        stream = io.BytesIO()
        self.assertEqual(
            write_warm_block(stream, study, self.trials, symbol_feeds_record=record),
            len(block),
        )
        self.assertEqual(stream.getvalue(), block)
        with self.assertRaisesRegex(WarmStartError, "record is required"):
            encode_warm_block(study, self.trials)
        rows[0]["space"]["symbol_feeds"] = {"symbols": {}}
        with self.assertRaisesRegex(WarmStartError, "differ between trial records"):
            encode_warm_block(study, rows)
        rows[0]["space"]["symbol_feeds"] = None
        with self.assertRaisesRegex(WarmStartError, "differ between trial records"):
            encode_warm_block(study, rows, symbol_feeds_record=record)
        with self.assertRaisesRegex(WarmStartError, "header exceeds 8 MiB"):
            encode_warm_block(
                study,
                self.trials,
                symbol_feeds_record={
                    "symbols": {"E": {"facts": "x" * (8 * 1024 * 1024)}}
                },
            )

    def test_empty_symbol_feeds_preserve_header_bytes(self):
        plain = encode_warm_block(self.study, self.trials)
        empty = replace(self.study, symbol_feeds={"symbols": {}})
        self.assertEqual(encode_warm_block(empty, self.trials), plain)
        empty_record = {
            "canonicalization": "pf-symbol-feed-barc-close-le-v1",
            "symbols": {},
        }
        self.assertEqual(
            encode_warm_block(empty, self.trials, symbol_feeds_record=empty_record),
            plain,
        )

    def test_other_checkpoint_versions_and_corruption(self):
        payload = "future opaque checkpoint\n"
        checksum = hashlib.sha256(payload.encode()).hexdigest()
        for version in (1, 2, 3, 12):
            state = f"PFHTPE{version}\n{checksum}\n{payload}"
            _validate_sampler_checkpoint(state)
            self.assertIn(
                state.encode(),
                encode_warm_block(self.study, self.trials, sampler_state=state),
            )
        for tag in ("PFHTPE0", "PFHTPE", "PFHTPE3x", "PFHTPE01"):
            with self.assertRaises(ValueError):
                _validate_sampler_checkpoint(f"{tag}\n{checksum}\n{payload}")
        with self.assertRaises(ValueError):
            _validate_sampler_checkpoint(f"PFHTPE3\n{checksum}\ncorrupt")

    def test_order_and_minimal_fields(self):
        rich = copy.deepcopy(self.trials)
        for trial in rich:
            trial.update(metrics={"discard": 1234}, error="discard", notes="discard")
        self.assertEqual(
            encode_warm_block(self.study, rich[::-1]),
            encode_warm_block(self.study, self.trials),
        )
        self.assertNotIn(b"discard", encode_warm_block(self.study, rich))

    def test_constraint_order_is_canonical(self):
        forward = replace(
            self.study,
            objective=replace(
                self.study.objective, constraints=("z expression", "a expression")
            ),
        )
        backward = replace(
            self.study,
            objective=replace(
                self.study.objective, constraints=("a expression", "z expression")
            ),
        )
        first, second = copy.deepcopy(self.trials), copy.deepcopy(self.trials)
        for left, right in zip(first, second):
            left["constraint_values"] = [1.0, -0.0]
            right["constraint_values"] = [-0.0, 1.0]
        self.assertEqual(
            encode_warm_block(forward, first), encode_warm_block(backward, second)
        )

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
            lambda rows: rows[0]["parameters"].update(
                b_stepped=math.nextafter(-0.75, 0.0)
            ),
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
