"""Pretrial validation, feed fingerprints, warm identity, and no-feed byte identity."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    from pineforge_hpo.study_spec import load_study_spec
    from pineforge_hpo.warm_binary import encode_warm_block

    parser = argparse.ArgumentParser()
    parser.add_argument("native", type=Path)
    parser.add_argument("plugin", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--harness", type=Path)
    args = parser.parse_args()
    oracle = None
    if args.harness:
        module = importlib.util.spec_from_file_location("release_harness", args.harness)
        oracle = importlib.util.module_from_spec(module)
        module.loader.exec_module(oracle)
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        chart = directory / "chart.csv"
        chart.write_text(
            "timestamp,open,high,low,close,volume\n"
            "1700000000000,100,102,99,101,10\n"
            "1700000060000,101,103,100,102,11\n"
        )
        feed = directory / "eth.csv"
        valid_csv = "timestamp,open,high,low,close,volume\n" + (
            "1700000000000,10,11,9,10,\n1700000060000,20,21,19,20,NaN\n"
        )
        feed.write_text(valid_csv)
        index = directory / "index.json"
        document = {
            "symbols": {
                "BINANCE:ETHUSDT": {
                    "syminfo": {
                        "tickerid": "BINANCE:ETHUSDT",
                        "type": "crypto",
                        "mintick": 0.01,
                    },
                    "feeds": {"1": "eth.csv"},
                }
            }
        }
        index.write_text(json.dumps(document))
        calls = 0

        def run(*extra, feeds=True, native=None, plugin=None, refused=None):
            nonlocal calls
            calls += 1
            trial_file = directory / f"trials-{calls}.jsonl"
            command = [
                str(native or args.native),
                "run",
                "--strategy",
                str(plugin or args.plugin),
                "--ohlcv",
                str(chart),
                "--objective",
                "metrics.all.net_profit",
                "--int-dim",
                "Length",
                "1",
                "99",
                "1",
                "--sampler",
                "grid",
                "--max-trials",
                "3",
                "--workers",
                "3",
                "--input-tf",
                "1",
                "--script-tf",
                "1" if feeds else "5",
                "--bar-magnifier",
                "true",
                "--magnifier-samples",
                "6",
                "--magnifier-distribution",
                "triangle",
                "--trials-file",
                str(trial_file),
            ]
            if feeds:
                command += ["--symbol-feeds", str(index)]
            process = subprocess.run(
                command + list(extra), capture_output=True, timeout=30
            )
            if refused:
                expected_exit = (
                    4
                    if refused.startswith(
                        ("symbol feeds differ", "invalid symbol feeds header record")
                    )
                    else 1
                )
                require(
                    process.returncode == expected_exit and not process.stdout,
                    f"refusal did not fail before trials: {process.stderr!r}",
                )
                require(refused.encode() in process.stderr, process.stderr.decode())
                require(not trial_file.exists(), "refusal opened terminal-trials file")
            else:
                require(process.returncode == 0, process.stderr.decode())
            return process, trial_file

        plain, _ = run(feeds=False)
        index.write_text('{"symbols":{}}')
        empty, _ = run("--symbol-feeds", str(index), feeds=False)
        require(
            plain.stdout == empty.stdout, "empty index changed no-feed result bytes"
        )
        if args.baseline:
            old, _ = run(feeds=False, native=args.baseline)
            require(
                old.stdout.replace(
                    b'"pineforge_hpo_version": "0.7.0"',
                    b'"pineforge_hpo_version": "0.8.0"',
                )
                == plain.stdout,
                "no-feed result differs from v0.7 beyond release version",
            )
        index.write_text(json.dumps(document))
        parent_process, parent_trials = run()
        parent = json.loads(parent_process.stdout)
        record = parent["applied_runtime"]["symbol_feeds"]
        if oracle:
            expected = oracle.symbol_feeds_record(oracle.load_symbol_feeds(index))
            require(record == expected, "engine release harness fingerprint differs")
        expected_hash = hashlib.sha256(b"pineforge:symbol-feed:barc-close-le:v1\0")
        for timestamp, close in [(1700000000000, 10), (1700000060000, 20)]:
            expected_hash.update(
                struct.pack(
                    "<5dqq",
                    close,
                    close + 1,
                    close - 1,
                    close,
                    float("nan"),
                    timestamp,
                    timestamp + 60000,
                )
            )
        require(
            record["symbols"]["BINANCE:ETHUSDT"]["feeds"]["1"]
            == {
                "bars": 2,
                "first_ts": 1700000000000,
                "last_ts": 1700000060000,
                "source_values_sha256": expected_hash.hexdigest(),
            },
            "value hash or feed bounds differ",
        )
        large_csv = (
            valid_csv.replace("volume\n", "volume,ignored\n")
            .replace("10,\n", "10,," + "x" * 131072 + "\n")
            .replace("20,NaN\n", "20,NaN," + "α" * 131072 + "\n")
        )
        multiline_csv = (
            valid_csv.replace("volume\n", "volume,ignored\n")
            .replace("10,\n", '10,,"' + "x" * 131070 + '\r\n"\n')
            .replace("20,NaN\n", "20,NaN,\n")
        )
        for numeric_csv in (
            valid_csv.translate(str.maketrans("0123456789", "０１２３４５６７８９")),
            valid_csv.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")),
            valid_csv.replace(",10,\n", ",\u00a010\u00a0,\u2007\n").replace(
                ",20,NaN", ",\u008520\u3000,\x1cNaN\x1f"
            ),
            valid_csv.replace("volume\n", "volume,time_close\n")
            .replace("10,\n", "10,\x1c,\x1d\n")
            .replace("20,NaN\n", "20,NaN,\u2007\n"),
            large_csv,
            multiline_csv,
            valid_csv.replace("10,\n", '"10\r\n",\n'),
        ):
            feed.write_text(numeric_csv)
            numeric_process, _ = run()
            require(
                json.loads(numeric_process.stdout)["applied_runtime"]["symbol_feeds"]
                == record,
                "numeric spellings or ignored fields changed feed value hash",
            )
            if oracle:
                require(
                    oracle.symbol_feeds_record(oracle.load_symbol_feeds(index))
                    == record,
                    "numeric CSV acceptance differs from release harness",
                )
        feed.write_text(valid_csv)
        parent_path = directory / "parent.json"
        parent_path.write_bytes(parent_process.stdout)
        spec = json.loads((ROOT / "examples/single_strategy/study.json").read_text())
        spec["strategies"][0]["search_space"] = {
            "Length": {"kind": "integer", "low": 1, "high": 99, "step": 1}
        }
        spec["objective"].update(expression="metrics.all.net_profit", constraints=[])
        spec["symbol_feeds"] = str(index)
        spec_path = directory / "work.json"
        spec_path.write_text(json.dumps(spec))
        inline_spec = copy.deepcopy(spec)
        inline_spec["symbol_feeds"] = document
        inline_path = directory / "inline-work.json"
        inline_path.write_text(json.dumps(inline_spec))
        inline_process, _ = run(
            "--symbol-feeds-spec",
            str(inline_path),
            "--script-tf",
            "1",
            feeds=False,
        )
        require(
            json.loads(inline_process.stdout)["applied_runtime"]["symbol_feeds"]
            == record,
            "work.json inline feeds differ from file index",
        )
        study = load_study_spec(spec_path)
        warm_bytes = encode_warm_block(study, parent["trials"])
        require(
            struct.unpack_from("<H", warm_bytes, 10)[0] == 1,
            "warm header did not advertise symbol feeds",
        )
        binary = directory / "parent.bin"
        binary.write_bytes(warm_bytes)
        for warm in (parent_path, parent_trials, binary):
            run("--warm-start", str(warm))
        joined_binary = directory / "joined.bin"
        joined_binary.write_bytes(
            encode_warm_block(study, parent["trials"][:1])
            + encode_warm_block(study, parent["trials"][1:])
        )
        run("--warm-start", str(joined_binary))
        native_binary = directory / "native.bin"
        encoded = subprocess.run(
            [
                str(args.native),
                "warm-encode",
                "--spec",
                str(spec_path),
                "--input",
                str(parent_path),
                "--output",
                str(native_binary),
            ],
            capture_output=True,
            timeout=30,
        )
        require(encoded.returncode == 0, encoded.stderr.decode())
        run("--warm-start", str(native_binary))
        override_spec = copy.deepcopy(spec)
        override_spec.pop("symbol_feeds")
        override_path = directory / "override-work.json"
        override_path.write_text(json.dumps(override_spec))
        override_binary = directory / "override.bin"
        override_encoded = subprocess.run(
            [
                str(args.native),
                "warm-encode",
                "--spec",
                str(override_path),
                "--symbol-feeds",
                str(index),
                "--input",
                str(parent_path),
                "--output",
                str(override_binary),
            ],
            capture_output=True,
            timeout=30,
        )
        require(override_encoded.returncode == 0, override_encoded.stderr.decode())
        require(
            override_binary.read_bytes() == native_binary.read_bytes(),
            "warm-encode CLI feed override changed header bytes",
        )
        info = subprocess.run(
            [
                str(args.native),
                "space-info",
                "--spec",
                str(override_path),
                "--symbol-feeds",
                str(index),
                "--warm-start",
                str(parent_path),
            ],
            capture_output=True,
            timeout=30,
        )
        require(info.returncode == 0, info.stderr.decode())
        require(
            json.loads(info.stdout)["space_hash"] == parent["space_hash"],
            "space-info CLI feed override changed space identity",
        )
        malformed_parent = copy.deepcopy(parent)
        malformed_parent["space"]["symbol_feeds"]["symbols"]["BINANCE:ETHUSDT"].pop(
            "facts"
        )
        malformed_path = directory / "malformed-header.json"
        malformed_path.write_text(json.dumps(malformed_parent))
        run(
            "--warm-start",
            str(malformed_path),
            refused="invalid symbol feeds header record for BINANCE:ETHUSDT",
        )
        tpe_process, _ = run("--sampler", "tpe")
        tpe_parent = json.loads(tpe_process.stdout)
        identity = tpe_parent["numeric_build_identity"]
        require(
            identity and tpe_parent["parent_numeric_build_identity"] is None,
            "fresh TPE identity provenance is missing",
        )
        tpe_path = directory / "tpe-parent.json"
        tpe_path.write_bytes(tpe_process.stdout)
        restored_process, _ = run("--sampler", "tpe", "--warm-start", str(tpe_path))
        restored = json.loads(restored_process.stdout)
        require(
            restored["warm_start_model"] == "restored_sampler_state"
            and restored["numeric_build_identity"] == identity
            and restored["parent_numeric_build_identity"] == identity,
            "restored TPE identity provenance differs",
        )
        require(
            restored["warm_start"]["numeric_build_identity"] == identity
            and restored["warm_start"]["parent_numeric_build_identity"] == identity,
            "warm provenance does not expose both identities",
        )
        tpe_binary = directory / "tpe-parent.bin"
        tpe_encoded = subprocess.run(
            [
                str(args.native),
                "warm-encode",
                "--spec",
                str(spec_path),
                "--input",
                str(tpe_path),
                "--output",
                str(tpe_binary),
            ],
            capture_output=True,
            timeout=30,
        )
        require(tpe_encoded.returncode == 0, tpe_encoded.stderr.decode())
        binary_process, _ = run("--sampler", "tpe", "--warm-start", str(tpe_binary))
        binary_restored = json.loads(binary_process.stdout)
        require(
            binary_restored["warm_start_model"] == "restored_sampler_state"
            and binary_restored["numeric_build_identity"] == identity
            and binary_restored["parent_numeric_build_identity"] == identity
            and binary_restored["warm_start"]["parent_numeric_build_identity"]
            == identity,
            "binary warm provenance lost the compared checkpoint identity",
        )
        foreign = copy.deepcopy(tpe_parent)
        version, checksum, payload = foreign["tpe_sampler_state"].split("\n", 2)
        payload = re.sub(
            r";libm_probe_sha256:[0-9a-f]{64}",
            ";libm_probe_sha256:" + "0" * 64,
            payload,
        )
        foreign["tpe_sampler_state"] = (
            version
            + "\n"
            + hashlib.sha256(payload.encode()).hexdigest()
            + "\n"
            + payload
        )
        foreign_path = directory / "foreign-tpe-parent.json"
        foreign_path.write_text(json.dumps(foreign))
        foreign_process, _ = run("--sampler", "tpe", "--warm-start", str(foreign_path))
        rebuilt = json.loads(foreign_process.stdout)
        require(
            rebuilt["warm_start_model"] == "rebuilt_history"
            and rebuilt["numeric_build_identity"] == identity
            and rebuilt["parent_numeric_build_identity"] != identity
            and ";libm_probe_sha256:" + "0" * 64
            in rebuilt["parent_numeric_build_identity"],
            "foreign checkpoint identity was not exposed before rebuilding",
        )
        future = copy.deepcopy(tpe_parent)
        future["tpe_sampler_state"] = future["tpe_sampler_state"].replace(
            "PFHTPE2\n", "PFHTPE3\n", 1
        )
        future_path = directory / "future-tpe-parent.json"
        future_path.write_text(json.dumps(future))
        future_process, _ = run("--sampler", "tpe", "--warm-start", str(future_path))
        future_result = json.loads(future_process.stdout)
        require(
            future_result["warm_start_model"] == "rebuilt_history"
            and future_result["parent_numeric_build_identity"] is None,
            "other checkpoint version did not rebuild with unknown parent identity",
        )
        for change in ("facts", "values", "closes", "removed", "key", "added"):
            changed = copy.deepcopy(document)
            if change == "facts":
                changed["symbols"]["BINANCE:ETHUSDT"]["syminfo"]["mintick"] = 0.02
            elif change == "values":
                feed.write_text(valid_csv.replace(",20,NaN", ",20.5,NaN"))
            elif change == "closes":
                feed.write_text(
                    valid_csv.replace("volume\n", "volume,time_close\n").replace(
                        "10,\n", "10,,1700000030000\n"
                    )
                )
            elif change == "removed":
                changed = {"symbols": {}}
            elif change == "key":
                changed["symbols"]["ETHUSDT"] = changed["symbols"].pop(
                    "BINANCE:ETHUSDT"
                )
            else:
                changed["symbols"]["BINANCE:BTCUSDT"] = {"feeds": {"1": "eth.csv"}}
            index.write_text(json.dumps(changed))
            for warm in (parent_path, parent_trials, binary, joined_binary):
                run(
                    "--warm-start", str(warm), refused="symbol feeds differ from parent"
                )
            feed.write_text(valid_csv)
        index.write_text(json.dumps(document))
        subnormal = copy.deepcopy(document)
        subnormal["symbols"]["BINANCE:ETHUSDT"]["syminfo"]["mintick"] = 5e-324
        index.write_text(json.dumps(subnormal))
        tiny_process, tiny_trials = run()
        tiny = json.loads(tiny_process.stdout)
        if oracle:
            require(
                tiny["applied_runtime"]["symbol_feeds"]
                == oracle.symbol_feeds_record(oracle.load_symbol_feeds(index)),
                "subnormal mintick differs from release harness",
            )
        tiny_parent = directory / "tiny-parent.json"
        tiny_parent.write_bytes(tiny_process.stdout)
        run("--warm-start", str(tiny_parent))
        run("--warm-start", str(tiny_trials))
        tiny_binary = directory / "tiny.bin"
        tiny_binary.write_bytes(encode_warm_block(study, tiny["trials"]))
        run("--warm-start", str(tiny_binary))
        index.write_text(json.dumps(document))
        relocated = directory / "relocated"
        relocated.mkdir()
        shutil.copy(feed, relocated / feed.name)
        (relocated / index.name).write_text(json.dumps(document))
        run("--symbol-feeds", str(relocated / index.name), "--warm-start", str(binary))
        large_index = {
            "symbols": {
                f"E{symbol}": {
                    "syminfo": {
                        field: "𐀀" * 256
                        for field in (
                            "tickerid",
                            "type",
                            "currency",
                            "session",
                            "timezone",
                        )
                    }
                }
                for symbol in range(256)
            }
        }
        large_spec = copy.deepcopy(spec)
        large_spec["symbol_feeds"] = large_index
        large_path = directory / "large-inline-work.json"
        large_path.write_text(json.dumps(large_spec, ensure_ascii=False))
        large_process, _ = run("--symbol-feeds-spec", str(large_path), feeds=False)
        large_parent = json.loads(large_process.stdout)
        large_record = large_parent["applied_runtime"]["symbol_feeds"]
        require(
            len(json.dumps(large_record, ensure_ascii=False).encode()) > 1024 * 1024,
            "large feed header fixture does not exceed the ordinary JSON frame limit",
        )
        if oracle:
            large_index_path = directory / "large-index.json"
            large_index_path.write_text(json.dumps(large_index, ensure_ascii=False))
            require(
                large_record
                == oracle.symbol_feeds_record(
                    oracle.load_symbol_feeds(large_index_path)
                ),
                "maximum-size symbol facts differ from the release harness",
            )
        large_binary = directory / "large-parent.bin"
        large_binary.write_bytes(
            encode_warm_block(load_study_spec(large_path), large_parent["trials"])
        )
        run(
            "--symbol-feeds-spec",
            str(large_path),
            "--warm-start",
            str(large_binary),
            feeds=False,
        )
        bad_indices = [
            (
                {"symbols": {"BINANCE:ETHUSDT": {"feeds": {"4h": "eth.csv"}}}},
                "ETHUSDT@4h",
            ),
            (
                {
                    "symbols": {
                        "BINANCE:ETHUSDT": {"feeds": {"D": "missing", "1D": "missing"}}
                    }
                },
                "two feeds at timeframe 1D",
            ),
            (
                {"symbols": {"BINANCE:ETHUSDT": {"feeds": {"1": "missing"}}}},
                "ETHUSDT@1",
            ),
            (
                {"symbols": {"BINANCE:ETHUSDT": {"syminfo": {"mintick": True}}}},
                "mintick",
            ),
            (
                {"symbols": {"BINANCE:ETHUSDT": {"syminfo": {"currency": 3}}}},
                "currency",
            ),
            ({"symbols": {"REFUSE": {"syminfo": {"currency": "USDT"}}}}, "REFUSE"),
            (
                {"symbols": {"REFUSE_FEED": {"feeds": {"1": "eth.csv"}}}},
                "REFUSE_FEED@1",
            ),
        ]
        for bad, diagnostic in bad_indices:
            index.write_text(json.dumps(bad))
            run(refused=diagnostic)
            if oracle and not any(key.startswith("REFUSE") for key in bad["symbols"]):
                try:
                    oracle.load_symbol_feeds(index)
                except oracle.SymbolFeedsError:
                    pass
                else:
                    raise AssertionError("native and release index validation differ")
        index.write_text(json.dumps(document))
        for csv in (
            valid_csv.replace("1700000060000", "1700000000000"),
            valid_csv.replace("1700000060000", "1700000030000"),
            valid_csv.replace(",10,\n", ",inf,\n"),
            valid_csv.replace(",20,NaN", ",20,-1"),
            valid_csv.replace("timestamp", "not_timestamp"),
            valid_csv.replace("1700000000000", "9007199254740992"),
            valid_csv.replace(",10,\n", ",\x1c10,\n"),
            large_csv.replace("x" * 131072, "x" * 131073),
            multiline_csv.replace("x" * 131070, "x" * 131071),
        ):
            feed.write_text(csv)
            run(refused="--symbol-feeds: feed BINANCE:ETHUSDT@1")
            if oracle:
                try:
                    oracle.load_symbol_feeds(index)
                except oracle.SymbolFeedsError:
                    pass
                else:
                    raise AssertionError("native and release CSV validation differ")
        feed.write_bytes(valid_csv.encode() + b"\xff")
        run(refused="UTF-8")
        index.write_text('{"symbols":{"E":{"feeds":{"1":"eth.csv","1":"eth.csv"}}}}')
        run(refused="duplicate key 1")
        symbol_artifact = directory / "symbol-artifact"
        symbol_artifact.mkdir()
        symbol_plugin = symbol_artifact / args.plugin.name
        shutil.copy(args.plugin, symbol_plugin)
        (symbol_artifact / "manifest.json").write_text(
            json.dumps(
                {
                    "input_kind_schema": 1,
                    "inputs": [{"title": "Other", "type": "string", "kind": "symbol"}],
                }
            )
        )
        run(
            "--categorical-choice",
            "Other",
            "BINANCE:ETHUSDT",
            feeds=False,
            plugin=symbol_plugin,
            refused="input.symbol",
        )
        manifest_path = symbol_artifact / "manifest.json"
        manifest_path.write_text("not JSON")
        optional, _ = run(feeds=False, plugin=symbol_plugin)
        require(
            optional.stdout == plain.stdout,
            "optional malformed manifest changed no-feed bytes",
        )
        run(
            "--categorical-choice",
            "Other",
            "ETH",
            feeds=False,
            plugin=symbol_plugin,
            refused=str(manifest_path),
        )
        manifest_path.unlink()
        run(
            "--categorical-choice",
            "Other",
            "ETH",
            feeds=False,
            plugin=symbol_plugin,
            refused="cannot rule out input.symbol",
        )
        manifest_path.write_text(
            json.dumps({"inputs": [{"title": "Other", "type": "string"}]})
        )
        run(
            "--categorical-choice",
            "Other",
            "ETH",
            feeds=False,
            plugin=symbol_plugin,
            refused="cannot rule out input.symbol",
        )
        modern_manifest = {
            "inputs": [{"title": "Other", "type": "string"}],
            "request_identity": {"codegen": {"version": "unknown"}},
        }
        manifest_path.write_text(json.dumps(modern_manifest))
        run(
            "--categorical-choice",
            "Other",
            "ETH",
            feeds=False,
            plugin=symbol_plugin,
            refused="manifest not stamped kind-capable by pineforge-hpo's builder",
        )
        modern_manifest["inputs"].append(
            {"title": "UnusedSymbol", "type": "string", "kind": "symbol"}
        )
        manifest_path.write_text(json.dumps(modern_manifest))
        run(
            "--categorical-choice",
            "Other",
            "ETH",
            feeds=False,
            plugin=symbol_plugin,
            refused="manifest not stamped kind-capable",
        )
        modern_manifest["inputs"].pop()
        for unrelated_kind in (None, 42, "string", "symbol"):
            modern_manifest["inputs"].append(
                {"title": "Unrelated", "type": "int", "kind": unrelated_kind}
            )
            manifest_path.write_text(json.dumps(modern_manifest))
            run(
                "--categorical-choice",
                "Other",
                "ETH",
                feeds=False,
                plugin=symbol_plugin,
                refused="manifest not stamped kind-capable",
            )
            modern_manifest["inputs"].pop()
        for marker in (True, "1", 1.0, 2, None):
            modern_manifest["input_kind_schema"] = marker
            manifest_path.write_text(json.dumps(modern_manifest))
            run(
                "--categorical-choice",
                "Other",
                "ETH",
                feeds=False,
                plugin=symbol_plugin,
                refused="manifest not stamped kind-capable",
            )
        modern_manifest["input_kind_schema"] = 1
        manifest_path.write_text(json.dumps(modern_manifest))
        run("--categorical-choice", "Other", "ETH", feeds=False, plugin=symbol_plugin)
        for kind in ("unknown", [], {}):
            modern_manifest["inputs"][0]["kind"] = kind
            manifest_path.write_text(json.dumps(modern_manifest))
            run(
                "--categorical-choice",
                "Other",
                "ETH",
                feeds=False,
                plugin=symbol_plugin,
                refused="cannot rule out input.symbol",
            )
        modern_manifest["inputs"][0] = {"title": "Other", "type": "foo"}
        manifest_path.write_text(json.dumps(modern_manifest))
        run(
            "--categorical-choice",
            "Other",
            "ETH",
            feeds=False,
            plugin=symbol_plugin,
            refused="search_space.Other.choices[0] is incompatible with Pine input type 'foo'",
        )
        for input_type in ("source", "enum"):
            manifest_path.write_text(
                json.dumps(
                    {
                        "inputs": [{"title": "Other", "type": input_type}],
                    }
                )
            )
            run(
                "--categorical-choice",
                "Other",
                "ETH",
                feeds=False,
                plugin=symbol_plugin,
            )
        manifest_path.write_text(
            json.dumps(
                {
                    "inputs": [{"title": "Unused", "type": "int"}] * 2,
                }
            )
        )
        duplicate_unused, _ = run(feeds=False, plugin=symbol_plugin)
        require(
            duplicate_unused.stdout == plain.stdout, "unrelated duplicate changed study"
        )
        manifest_path.write_text(
            json.dumps(
                {
                    "inputs": [{"title": "Length", "type": "int"}] * 2,
                }
            )
        )
        run(
            feeds=False,
            plugin=symbol_plugin,
            refused="duplicate input Length used by search",
        )
        print(
            "PASS metadata: marker-only capability; unrelated kinds cannot vouch; "
            "unknown types refused; source/enum and duplicates checked"
        )
        large_manifest = {
            "input_kind_schema": 1,
            "inputs": [{"title": "Other", "type": "string", "kind": "string"}],
            "padding": "x" * (1024 * 1024 + 1),
        }
        manifest_path.write_text(json.dumps(large_manifest))
        optional, _ = run(feeds=False, plugin=symbol_plugin)
        require(
            optional.stdout == plain.stdout,
            "large optional manifest changed no-feed bytes",
        )
        run("--categorical-choice", "Other", "ETH", feeds=False, plugin=symbol_plugin)
        if oracle:
            index.write_text(json.dumps(document))
            for csv in (
                "unused\n",
                valid_csv.replace("1700000060000", "1700000030000"),
            ):
                feed.write_text(csv)
                try:
                    oracle.load_symbol_feeds(index)
                except oracle.SymbolFeedsError as error:
                    run(refused=str(error))
                else:
                    raise AssertionError("error-parity fixture unexpectedly accepted")
            feed.write_text(valid_csv)
            missing_index = directory / "missing-index.json"
            try:
                oracle.load_symbol_feeds(missing_index)
            except oracle.SymbolFeedsError as error:
                run("--symbol-feeds", str(missing_index), refused=str(error))
        print(
            "PASS: feed hash/reference, pretrial refusals, D7, JSON/JSONL/v2 mismatch, "
            "relocation, empty-index byte identity, manifest compatibility, "
            "CLI feed overrides, clear header errors, numeric identity provenance"
            + (", v0.7 byte identity" if args.baseline else "")
        )


if __name__ == "__main__":
    main()
