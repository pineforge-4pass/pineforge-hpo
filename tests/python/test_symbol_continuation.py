from __future__ import annotations

import contextlib
import copy
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

from pineforge_hpo.cli import main
from pineforge_hpo.continuation import (
    ContinuationDataContext,
    WarmStartError,
    load_warm_start,
)
from pineforge_hpo.study_spec import load_study_spec
from pineforge_hpo.warm_binary import (
    decode_warm_blocks,
    encode_warm_block,
    write_warm_block,
)
import symbol_continuation_oracle as oracle


class SymbolContinuationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        document = {
            "schema_version": 1,
            "mode": "single_strategy",
            "strategies": [
                {
                    "id": "strategy",
                    "source": "strategy.pine",
                    "datasets": ["first", "second"],
                    "search_space": oracle.space()["parameters"],
                }
            ],
            "datasets": [
                {"id": name, "ohlcv": name + ".csv", "input_tf": "60"}
                for name in ("second", "first")
            ],
            "objective": {"kind": "expression", **oracle.space()["objective"]},
            "sampler": {"kind": "grid", "seed": 17, "trials": 2},
            "execution": {"workers": 1, "isolation": "threads", "pruner": "none"},
        }
        path = self.root / "study.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        self.study = load_study_spec(path)
        self.parent = self.root / "parent.json"
        self.context = ContinuationDataContext(oracle.canonical(oracle.descriptor()))

    def assert_rejected(self, function) -> None:
        with self.assertRaises(WarmStartError) as raised:
            function()
        self.assertEqual(raised.exception.code, "hpo_warm_start_rejected")
        self.assertEqual(raised.exception.failure_args, {})

    def write_parent(self, form: str, document: dict) -> None:
        if form == "result":
            encoded = oracle.canonical(document)
        elif form == "array":
            encoded = oracle.canonical(document["trials"])
        else:
            encoded = "".join(
                oracle.canonical(row) + "\n" for row in document["trials"]
            )
        self.parent.write_text(encoded, encoding="utf-8")

    def test_complete_context_and_json_formats_use_independent_bytes(self) -> None:
        self.assertEqual(
            self.context.canonical_descriptor, oracle.canonical(oracle.descriptor())
        )
        self.assertEqual(
            self.context.data_context_sha256, oracle.digest(oracle.descriptor())
        )
        for form in ("result", "array", "jsonl"):
            with self.subTest(form=form):
                self.write_parent(form, oracle.result())
                history = load_warm_start(
                    self.study, self.parent, data_context=self.context
                )
                self.assertEqual(
                    (history.next_id, history.completed, history.feasible), (20, 2, 2)
                )
                self.assertEqual(len(history.tried), 2)
                self.assertEqual(
                    [row["dataset_id"] for row in history.trials], ["first", "first"]
                )
                tapes = {}
                for name, price in (("first", 1), ("second", 2)):
                    tapes[name] = self.root / ("moved-" + name + ".csv")
                    tapes[name].write_text(
                        "timestamp,open,high,low,close,volume\n"
                        f"0,{price},{price},{price},{price},{price}\n",
                        encoding="utf-8",
                    )
                relocated = ContinuationDataContext(
                    oracle.canonical(oracle.descriptor(tapes))
                )
                self.assertEqual(
                    load_warm_start(self.study, self.parent, data_context=relocated),
                    history,
                )
                tapes["second"].write_text(
                    "timestamp,open,high,low,close,volume\n0,3,3,3,3,3\n",
                    encoding="utf-8",
                )
                changed = ContinuationDataContext(
                    oracle.canonical(oracle.descriptor(tapes))
                )
                self.assert_rejected(
                    lambda: load_warm_start(
                        self.study, self.parent, data_context=changed
                    )
                )

    def test_every_unsampled_context_axis_is_checked_in_all_json_forms(self) -> None:
        changes = (
            lambda value: value["bindings"]["Market"]["datasets"].update(
                {"asset-b": "first"}
            ),
            lambda value: value["datasets"]["second"].update(values_sha256="b" * 64),
            lambda value: value["datasets"]["second"]["facts"].update(
                session="0900-1700"
            ),
            lambda value: value["fixed_inputs"].update(Count={"int": "3"}),
            lambda value: value["overrides"].update(slippage={"int": "1"}),
            lambda value: value["bindings"]["Market"].update(role="request"),
            lambda value: value["comparison"].update(warmup="other"),
            lambda value: value.update(plugin_sha256="b" * 64),
        )
        for form in ("result", "array", "jsonl"):
            self.write_parent(form, oracle.result())
            for change in changes:
                value = oracle.descriptor()
                change(value)
                with self.subTest(form=form, change=changes.index(change)):
                    self.assert_rejected(
                        lambda: load_warm_start(
                            self.study,
                            self.parent,
                            data_context=ContinuationDataContext(
                                oracle.canonical(value)
                            ),
                        )
                    )
                    self.assert_rejected(
                        lambda: decode_warm_blocks(
                            self.study,
                            oracle.binary(),
                            data_context=ContinuationDataContext(
                                oracle.canonical(value)
                            ),
                        )
                    )

    def test_forged_labels_missing_identity_and_dropped_binding_refuse(self) -> None:
        for form in ("result", "array", "jsonl"):
            for field, value in (
                ("symbol", "asset-b"),
                ("dataset_id", "second"),
                ("data_context_sha256", "0" * 64),
                ("data_context_sha256", None),
            ):
                document = oracle.result()
                if value is None:
                    del document["trials"][0][field]
                else:
                    document["trials"][0][field] = value
                self.write_parent(form, document)
                with self.subTest(form=form, field=field, value=value):
                    self.assert_rejected(
                        lambda: load_warm_start(
                            self.study, self.parent, data_context=self.context
                        )
                    )
            self.write_parent(form, oracle.result())
            self.assert_rejected(lambda: load_warm_start(self.study, self.parent))
        for field in ("data_context_sha256", "dataset_ids", "applied_runtime"):
            document = oracle.result()
            del document[field]
            self.write_parent("result", document)
            self.assert_rejected(
                lambda: load_warm_start(
                    self.study, self.parent, data_context=self.context
                )
            )
        for ids in (["first"], ["first", "first"], ["first", "unknown"]):
            document = oracle.result()
            document["dataset_ids"] = ids
            self.write_parent("result", document)
            self.assert_rejected(
                lambda: load_warm_start(
                    self.study, self.parent, data_context=self.context
                )
            )
        document = oracle.result()
        document["applied_runtime"]["data_context"]["fixed_inputs"]["Enabled"] = False
        self.write_parent("result", document)
        self.assert_rejected(
            lambda: load_warm_start(self.study, self.parent, data_context=self.context)
        )
        document = oracle.result()
        for name in ("applied_runtime", "dataset_ids", "data_context_sha256"):
            del document[name]
        for row in document["trials"]:
            del row["data_context_sha256"]
        self.write_parent("result", document)
        self.assert_rejected(lambda: load_warm_start(self.study, self.parent))

    def test_complete_ancestry_is_checked(self) -> None:
        document = oracle.result()
        document["warm_start_trials"] = [document["trials"].pop(0)]
        document["trials_completed"] = 1
        document["warm_start"] = {"trials": 1}
        self.write_parent("result", document)
        history = load_warm_start(self.study, self.parent, data_context=self.context)
        self.assertEqual((len(history.trials), history.next_id), (2, 20))
        document["warm_start_trials"][0]["dataset_id"] = "second"
        self.write_parent("result", document)
        self.assert_rejected(
            lambda: load_warm_start(self.study, self.parent, data_context=self.context)
        )

    def test_dropped_binding_preflight_returns_exit_four_before_build(self) -> None:
        self.write_parent("result", oracle.result())
        with (
            contextlib.redirect_stderr(io.StringIO()),
            mock.patch("pineforge_hpo.cli.ArtifactBuilder") as builder,
        ):
            self.assertEqual(
                main(
                    ["run", str(self.study.spec_path), "--warm-start", str(self.parent)]
                ),
                4,
            )
            builder.assert_not_called()

    def test_bound_binary_writer_and_reader_match_independent_columns(self) -> None:
        rows = oracle.result()["trials"]
        expected = oracle.binary()
        actual = encode_warm_block(self.study, rows, data_context=self.context)
        self.assertEqual(actual, expected)
        output = io.BytesIO()
        self.assertEqual(
            write_warm_block(output, self.study, rows, data_context=self.context),
            len(expected),
        )
        self.assertEqual(output.getvalue(), expected)
        decoded = decode_warm_blocks(self.study, actual, data_context=self.context)
        self.assertEqual(oracle.canonical(decoded), oracle.canonical(oracle.rows()))
        self.assertEqual(struct.unpack_from("<H", actual, 10)[0], 2)
        # A bound writer omits the redundant feed extension, even if supplied by its caller.
        self.assertEqual(
            encode_warm_block(
                self.study,
                rows,
                data_context=self.context,
                symbol_feeds_record={"symbols": {}},
            ),
            expected,
        )

    def test_binary_context_and_checked_lengths_refuse_before_rows(self) -> None:
        value = oracle.descriptor()
        value["datasets"]["second"]["values_sha256"] = "b" * 64
        other = ContinuationDataContext(oracle.canonical(value))
        self.assert_rejected(
            lambda: decode_warm_blocks(self.study, oracle.binary(), data_context=other)
        )
        self.assert_rejected(lambda: decode_warm_blocks(self.study, oracle.binary()))
        self.assert_rejected(
            lambda: decode_warm_blocks(
                self.study, oracle.binary(bound=False), data_context=self.context
            )
        )
        for offset in (10, 12, 16, 88, 92):
            malformed = bytearray(oracle.binary())
            malformed[offset] ^= 0x80
            self.assert_rejected(
                lambda: decode_warm_blocks(
                    self.study, malformed, data_context=self.context
                )
            )
        for size in range(88, 124):
            self.assert_rejected(
                lambda: decode_warm_blocks(
                    self.study, oracle.binary()[:size], data_context=self.context
                )
            )
        self.assert_rejected(
            lambda: decode_warm_blocks(
                self.study,
                oracle.binary() + oracle.binary(bound=False),
                data_context=self.context,
            )
        )
        decoded = decode_warm_blocks(
            self.study,
            encode_warm_block(
                self.study, [oracle.result()["trials"][0]], data_context=self.context
            )
            + encode_warm_block(
                self.study, [oracle.result()["trials"][1]], data_context=self.context
            ),
            data_context=self.context,
        )
        self.assertEqual([row["trial_id"] for row in decoded], [7, 19])

    def test_bound_binary_replaces_feed_block_after_complete_context_check(
        self,
    ) -> None:
        feeds = {
            "symbols": {"reference": {"facts": {"currency": "units"}, "feeds": {}}}
        }
        value = oracle.descriptor()
        value["symbol_feeds"] = feeds
        context = ContinuationDataContext(oracle.canonical(value))
        rows = oracle.rows()
        for row in rows:
            row["data_context_sha256"] = oracle.digest(value)
        encoded = encode_warm_block(
            self.study, rows, data_context=context, symbol_feeds_record=feeds
        )
        self.assertEqual(struct.unpack_from("<H", encoded, 10)[0], 2)
        self.assertEqual(
            decode_warm_blocks(
                self.study, encoded, data_context=context, symbol_feeds_record=feeds
            ),
            tuple(rows),
        )
        self.assert_rejected(
            lambda: decode_warm_blocks(
                self.study,
                encoded,
                data_context=self.context,
                symbol_feeds_record=feeds,
            )
        )

    def test_binary_dual_extensions_both_match(self) -> None:
        feeds = {
            "symbols": {"reference": {"facts": {"currency": "units"}, "feeds": {}}}
        }
        encoded = oracle.binary(feeds=feeds)
        decoded = decode_warm_blocks(
            self.study, encoded, data_context=self.context, symbol_feeds_record=feeds
        )
        self.assertEqual([row["trial_id"] for row in decoded], [7, 19])
        changed = copy.deepcopy(feeds)
        changed["symbols"]["reference"]["facts"]["currency"] = "different"
        self.assert_rejected(
            lambda: decode_warm_blocks(
                self.study,
                encoded,
                data_context=self.context,
                symbol_feeds_record=changed,
            )
        )

    def test_unbound_binary_bytes_remain_the_independent_legacy_format(self) -> None:
        rows = [
            {
                key: value
                for key, value in row.items()
                if key not in ("symbol", "dataset_id", "data_context_sha256")
            }
            for row in oracle.result()["trials"]
        ]
        self.assertEqual(
            encode_warm_block(self.study, rows), oracle.binary(bound=False)
        )
        self.assert_rejected(
            lambda: encode_warm_block(self.study, oracle.result()["trials"])
        )
        forged = copy.deepcopy(oracle.result()["trials"])
        forged[0]["symbol"] = "asset-b"
        self.assert_rejected(
            lambda: encode_warm_block(self.study, forged, data_context=self.context)
        )


if __name__ == "__main__":
    unittest.main()
