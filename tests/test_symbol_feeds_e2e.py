"""Actual Pine + C ABI: BTCUSDT 4h, ETHUSDT 240/1D, three byte-identical candidates."""

from __future__ import annotations

import argparse
import copy
import ctypes
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "external/pineforge-codegen-oss"))


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def canonical_abi_bytes(raw, structure):
    size = ctypes.sizeof(structure)
    require(len(raw) % size == 0, "C-ABI record size differs from the release harness")
    ranges = []

    def fields(record_type, base):
        for name, field_type in record_type._fields_:
            offset = base + getattr(record_type, name).offset
            if issubclass(field_type, ctypes.Structure):
                fields(field_type, offset)
            else:
                ranges.append((offset, ctypes.sizeof(field_type)))

    fields(structure, 0)
    canonical = bytearray(len(raw))
    for base in range(0, len(raw), size):
        for offset, length in ranges:
            start = base + offset
            canonical[start : start + length] = raw[start : start + length]
    return bytes(canonical)


def oracle_probe():
    harness, output = Path(sys.argv[2]), Path(sys.argv[3])
    module = importlib.util.spec_from_file_location("release_harness", harness)
    oracle = importlib.util.module_from_spec(module)
    module.loader.exec_module(oracle)
    original = oracle.build_report_dict

    def capture(report, *args, **kwargs):
        output.mkdir(parents=True, exist_ok=True)
        (output / "metrics.bin").write_bytes(
            ctypes.string_at(
                ctypes.addressof(report.metrics), ctypes.sizeof(report.metrics)
            )
        )
        (output / "trades.bin").write_bytes(
            ctypes.string_at(
                report.trades, report.total_trades * ctypes.sizeof(oracle.TradeC)
            )
        )
        return original(report, *args, **kwargs)

    oracle.build_report_dict = capture
    sys.argv = [str(harness), *sys.argv[4:]]
    return oracle.main()


def main():
    from pineforge_hpo.cli import prepare_run

    parser = argparse.ArgumentParser()
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--harness", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    module = importlib.util.spec_from_file_location("release_abi", args.harness)
    abi = importlib.util.module_from_spec(module)
    module.loader.exec_module(abi)
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    first = 1704067200000

    def write_csv(name, count, milliseconds, price):
        path = directory / name
        rows = ["timestamp,open,high,low,close,volume"]
        for index in range(count):
            close = price(index)
            rows.append(
                f"{first + index * milliseconds},{close - 2},{close + 5},"
                f"{close - 5},{close},100"
            )
        path.write_text("\n".join(rows) + "\n")
        return path

    chart = write_csv(
        "btcusdt-240.csv",
        1440,
        14400000,
        lambda index: 40000 + 3000 * math.sin(index * 0.09),
    )
    write_csv(
        "ethusdt-240.csv",
        1440,
        14400000,
        lambda index: 1850 + 200 * math.sin(index * 0.11),
    )
    write_csv(
        "ethusdt-1D.csv",
        240,
        86400000,
        lambda index: 1840 + 70 * math.sin(index * 0.13),
    )
    index = directory / "feeds.json"
    index.write_text(
        json.dumps(
            {
                "symbols": {
                    "BINANCE:ETHUSDT": {
                        "syminfo": {
                            "tickerid": "BINANCE:ETHUSDT",
                            "type": "crypto",
                            "currency": "USDT",
                            "mintick": 0.01,
                            "timezone": "UTC",
                            "session": "24x7",
                        },
                        "feeds": {"240": "ethusdt-240.csv", "1D": "ethusdt-1D.csv"},
                    }
                }
            }
        )
    )
    syminfo = directory / "syminfo.json"
    syminfo.write_text('{"mintick":0.01}')
    source = directory / "btc-eth.pine"
    source.write_text("""//@version=6
strategy("BTC ETH feed HPO", overlay=true, initial_capital=100000)
threshold = input.float(1850.0, "ETH threshold", minval=1700.0, maxval=2000.0)
other = input.symbol("BINANCE:ETHUSDT", "Other")
eth4 = request.security(other, "240", close)
ethD = request.security(other, "1D", close)
if eth4 > threshold and eth4 > ethD
    strategy.entry("Long", strategy.long)
if eth4 < threshold or eth4 <= ethD
    strategy.close("Long")
""")
    document = json.loads((ROOT / "examples/single_strategy/study.json").read_text())
    document["strategies"][0].update(
        source=str(source),
        fixed_inputs={"Other": "BINANCE:ETHUSDT"},
        strategy_overrides={},
        search_space={
            "ETH threshold": {
                "kind": "real",
                "low": 1750.0,
                "high": 1950.0,
                "step": 100.0,
            }
        },
    )
    document["datasets"][0].update(
        ohlcv=str(chart),
        input_tf="240",
        script_tf="240",
        chart_timezone="UTC",
    )
    document["symbol_feeds"] = str(index)
    document["objective"].update(expression="metrics.all.net_profit", constraints=[])
    document["sampler"].update(kind="grid", trials=3)
    document["execution"].update(workers=3, batch_size=3, pruner="none")
    spec = directory / "work.json"
    spec.write_text(json.dumps(document))
    command, artifact = prepare_run(
        spec,
        ROOT / "external/pineforge-engine",
        directory / "cache",
        native=args.native,
    )
    command += ["--syminfo", str(syminfo)]
    manifest = json.loads(Path(artifact["manifest"]).read_text())
    require(
        any(
            item["title"] == "Other" and item.get("kind") == "symbol"
            for item in manifest["inputs"]
        ),
        "real codegen artifact does not identify input.symbol",
    )
    symbol_search = copy.deepcopy(document)
    symbol_search["strategies"][0]["fixed_inputs"] = {}
    symbol_search["strategies"][0]["search_space"]["Other"] = {
        "kind": "categorical",
        "choices": ["BINANCE:ETHUSDT", "BINANCE:BTCUSDT"],
    }
    refused_spec = directory / "symbol-search.json"
    refused_spec.write_text(json.dumps(symbol_search))
    from pineforge_hpo.cli import CliError

    try:
        prepare_run(
            refused_spec,
            ROOT / "external/pineforge-engine",
            directory / "cache",
            native=args.native,
        )
    except CliError as error:
        require("input.symbol" in str(error) and "D7" in str(error), str(error))
    else:
        raise AssertionError(
            "real input.symbol artifact accepted as a search dimension"
        )
    refused_trials = directory / "d7-trials.jsonl"
    refused = subprocess.run(
        command
        + [
            "--categorical-choice",
            "Other",
            "BINANCE:ETHUSDT",
            "--categorical-choice",
            "Other",
            "BINANCE:BTCUSDT",
            "--trials-file",
            str(refused_trials),
        ],
        capture_output=True,
        timeout=30,
    )
    require(
        refused.returncode == 1
        and b"input.symbol" in refused.stderr
        and b"D7" in refused.stderr
        and not refused.stdout
        and not refused_trials.exists(),
        refused.stderr.decode(),
    )
    print(
        "PASS D7: real codegen v1.2.0 input.symbol artifact refused by Python and native",
        flush=True,
    )
    run = subprocess.run(command, capture_output=True, timeout=180)
    require(run.returncode == 0, run.stderr.decode())
    (directory / "hpo.json").write_bytes(run.stdout)
    result = json.loads(run.stdout)
    evidence = []
    for trial in result["trials"]:
        candidate = trial["parameters"]["ETH threshold"]
        native_dir = directory / f"native-{candidate:g}"
        oracle_dir = directory / f"oracle-{candidate:g}"
        probe = subprocess.run(
            [
                str(args.probe),
                artifact["plugin"],
                str(chart),
                str(index),
                str(candidate),
                str(native_dir),
            ],
            capture_output=True,
            timeout=60,
        )
        require(probe.returncode == 0, probe.stderr.decode())
        canonical = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--oracle-probe",
                str(args.harness),
                str(oracle_dir),
                "--so",
                artifact["plugin"],
                "--ohlcv",
                str(chart),
                "--inputs",
                json.dumps({"ETH threshold": candidate, "Other": "BINANCE:ETHUSDT"}),
                "--input-tf",
                "240",
                "--script-tf",
                "240",
                "--chart-tz",
                "UTC",
                "--syminfo",
                str(syminfo),
                "--symbol-feeds",
                str(index),
            ],
            capture_output=True,
            timeout=60,
        )
        require(canonical.returncode == 0, canonical.stderr.decode())
        (oracle_dir / "report.json").write_bytes(canonical.stdout)
        expected = json.loads(canonical.stdout)
        require(
            trial["total_trades"] == int(probe.stdout) == len(expected["trades"]) > 0,
            "trial/probe/release harness trade counts differ or fixture is vacuous",
        )
        require(
            struct.pack("<d", trial["objective"])
            == struct.pack("<d", expected["metrics"]["all"]["net_profit"]),
            "trial/release metric bits differ",
        )
        require(
            result["applied_runtime"]["symbol_feeds"]
            == expected["applied_runtime"]["symbol_feeds"],
            "feed fingerprints differ",
        )
        row = {"candidate": candidate, "trades": trial["total_trades"]}
        for kind in ("metrics", "trades"):
            actual_raw = (native_dir / f"{kind}.bin").read_bytes()
            reference_raw = (oracle_dir / f"{kind}.bin").read_bytes()
            if kind == "metrics":
                require(
                    actual_raw == reference_raw,
                    f"candidate {candidate}: raw C-ABI metrics bytes differ",
                )
            structure = abi.MetricsC if kind == "metrics" else abi.TradeC
            actual = canonical_abi_bytes(actual_raw, structure)
            reference = canonical_abi_bytes(reference_raw, structure)
            (native_dir / f"{kind}.canonical.bin").write_bytes(actual)
            (oracle_dir / f"{kind}.canonical.bin").write_bytes(reference)
            require(
                actual == reference, f"candidate {candidate}: C-ABI {kind} bytes differ"
            )
            row[kind + "_bytes"] = len(actual)
            row[kind + "_sha256"] = hashlib.sha256(actual).hexdigest()
            row[kind + "_raw_native_sha256"] = hashlib.sha256(actual_raw).hexdigest()
            row[kind + "_raw_oracle_sha256"] = hashlib.sha256(reference_raw).hexdigest()
        evidence.append(row)
        print("PASS C-ABI equality: " + json.dumps(row), flush=True)
    require(len(evidence) == 3, "expected three distinct candidates")
    metadata = {
        "fixture": "deterministic synthetic BTCUSDT 4h / ETHUSDT 240 + 1D",
        "comparison": "all C-ABI field bytes unchanged; only unspecified struct padding zeroed",
        "artifact": artifact,
        "d7_real_artifact_refused": True,
        "harness_sha256": hashlib.sha256(args.harness.read_bytes()).hexdigest(),
        "candidates": evidence,
        "files": {},
    }
    for path in (
        chart,
        source,
        index,
        directory / "ethusdt-240.csv",
        directory / "ethusdt-1D.csv",
    ):
        metadata["files"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (directory / "equality-evidence.json").write_text(
        json.dumps(metadata, indent=2) + "\n"
    )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--oracle-probe":
        raise SystemExit(oracle_probe())
    main()
