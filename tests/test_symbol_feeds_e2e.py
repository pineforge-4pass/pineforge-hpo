"""Actual Pine + C ABI: BTCUSDT 4h, ETHUSDT 240/1D, three byte-identical candidates."""

from __future__ import annotations

import argparse
import copy
import ctypes
import hashlib
from importlib import metadata
import importlib.util
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
from unittest import mock

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


def input_metadata_probe(args, directory, document):
    from pineforge_hpo.cli import CliError, prepare_run

    source = directory / "input-kinds.pine"
    source.write_text("""//@version=6
strategy("Input metadata HPO", initial_capital=100000)
enum Side
    long
    short
selected = input.source(close, "Source")
side = input.enum(Side.long, "Side")
mode = input.string("fast", "Mode", options=["fast", "slow"])
other = input.symbol("BINANCE:ETHUSDT", "Other")
if selected > open and mode == "fast"
    if side == Side.long
        strategy.entry("Long", strategy.long)
    else
        strategy.entry("Short", strategy.short)
if selected <= open
    strategy.close_all()
""")
    work = copy.deepcopy(document)
    work.pop("symbol_feeds", None)
    work["strategies"][0].update(
        source=str(source),
        fixed_inputs={"Other": "BINANCE:ETHUSDT"},
        search_space={
            "Source": {"kind": "categorical", "choices": ["close", "open", "hl2"]},
            "Side": {"kind": "categorical", "choices": [0, 1]},
            "Mode": {"kind": "categorical", "choices": ["fast", "slow"]},
        },
    )
    work["sampler"].update(kind="grid", trials=12)
    spec = directory / "input-kinds.json"
    spec.write_text(json.dumps(work))
    with mock.patch(
        "pineforge_hpo.transpile.metadata.version",
        side_effect=metadata.PackageNotFoundError("pineforge-codegen"),
    ):
        command, artifact = prepare_run(
            spec,
            ROOT / "external/pineforge-engine",
            directory / "cache",
            native=args.native,
        )
    manifest_path = Path(artifact["manifest"])
    manifest = json.loads(manifest_path.read_text())
    require(
        manifest["request_identity"]["codegen"]["version"] == "unknown",
        "missing source-checkout package metadata did not produce unknown version",
    )
    require(manifest.get("input_kind_schema") == 1, "builder did not stamp capability")
    (directory / "input-kinds-manifest.json").write_text(json.dumps(manifest, indent=2))
    types = {item["title"]: item["type"] for item in manifest["inputs"]}
    require(types["Source"] == "source" and types["Side"] == "enum", types)
    result = subprocess.run(command, capture_output=True, timeout=180)
    require(result.returncode == 0, result.stderr.decode())
    trials = json.loads(result.stdout)["trials"]
    require(
        len(trials) == 12 and all(trial["status"] == "ok" for trial in trials), trials
    )
    require(
        {trial["parameters"]["Source"] for trial in trials} == {"close", "open", "hl2"},
        trials,
    )
    require({trial["parameters"]["Side"] for trial in trials} == {0, 1}, trials)
    (directory / "input-kinds-result.json").write_bytes(result.stdout)
    print(
        "PASS input.source/input.enum: real v1.2.0 artifact, Python + native, 12 trials",
        flush=True,
    )

    work["strategies"][0].pop("source")
    work["strategies"][0]["artifact"] = artifact["plugin"]
    spec.write_text(json.dumps(work))
    unknown_command, _ = prepare_run(
        spec,
        ROOT / "external/pineforge-engine",
        directory / "cache",
        native=args.native,
    )
    unknown = subprocess.run(unknown_command, capture_output=True, timeout=180)
    require(
        unknown.returncode == 0 and unknown.stdout == result.stdout,
        unknown.stderr.decode(),
    )
    manifest["inputs"] = [
        item for item in manifest["inputs"] if item["title"] != "Other"
    ]
    manifest_path.write_text(json.dumps(manifest))
    work["strategies"][0]["fixed_inputs"] = {}
    spec.write_text(json.dumps(work))
    symbol_free_command, _ = prepare_run(
        spec,
        ROOT / "external/pineforge-engine",
        directory / "cache",
        native=args.native,
    )
    symbol_free = subprocess.run(symbol_free_command, capture_output=True, timeout=180)
    require(symbol_free.returncode == 0, symbol_free.stderr.decode())
    manifest.pop("input_kind_schema")
    manifest_path.write_text(json.dumps(manifest))
    provenance_path = Path(artifact["provenance"])
    provenance = json.loads(provenance_path.read_text())
    provenance.pop("input_kind_schema")
    provenance_path.write_text(json.dumps(provenance))
    try:
        prepare_run(
            spec,
            ROOT / "external/pineforge-engine",
            directory / "cache",
            native=args.native,
        )
    except CliError as error:
        require("manifest not stamped kind-capable" in str(error), str(error))
        require("codegen version unknown" in str(error), str(error))
    else:
        raise AssertionError("Python accepted an unstamped manifest")
    refused = subprocess.run(symbol_free_command, capture_output=True, timeout=30)
    require(
        refused.returncode == 1
        and b"manifest not stamped kind-capable" in refused.stderr
        and b"codegen version unknown" in refused.stderr
        and json.loads(refused.stdout)["ok"] is False,
        refused.stderr.decode(),
    )
    print(
        "PASS unknown codegen: stamped symbol-free metadata accepted; "
        "unstamped manifest refused in both frontends",
        flush=True,
    )


def symbol_free_string_probe(args, directory, document):
    from pineforge_hpo.cli import CliError, prepare_run

    source = directory / "symbol-free-strings.pine"
    source.write_text("""//@version=6
strategy("Symbol-free string HPO", initial_capital=100000)
selected = input.source(close, "Source")
mode = input.string("fast", "Mode", options=["fast", "slow"])
period = input.timeframe("240", "Timeframe", options=["240", "1D"])
length = input.int(1, "Length", minval=1, maxval=2)
if selected > open and mode == "fast" and period == "240" and bar_index >= length
    strategy.entry("Long", strategy.long)
if selected <= open
    strategy.close_all()
""")
    work = copy.deepcopy(document)
    work.pop("symbol_feeds", None)
    work["strategies"][0].update(
        source=str(source),
        fixed_inputs={"Source": "close"},
        search_space={
            "Mode": {"kind": "categorical", "choices": ["fast", "slow"]},
            "Timeframe": {"kind": "categorical", "choices": ["240", "1D"]},
            "Length": {"kind": "integer", "low": 1, "high": 2, "step": 1},
        },
    )
    work["sampler"].update(kind="grid", trials=8)
    spec = directory / "symbol-free-strings.json"
    spec.write_text(json.dumps(work))
    command, artifact = prepare_run(
        spec,
        ROOT / "external/pineforge-engine",
        directory / "cache",
        native=args.native,
    )
    manifest_path = Path(artifact["manifest"])
    manifest = json.loads(manifest_path.read_text())
    require(manifest.get("input_kind_schema") == 1, "symbol-free artifact lacks marker")
    require(all("kind" not in item for item in manifest["inputs"]), manifest["inputs"])
    (directory / "symbol-free-strings-manifest.json").write_text(
        json.dumps(manifest, indent=2)
    )
    result = subprocess.run(command, capture_output=True, timeout=180)
    require(result.returncode == 0, result.stderr.decode())
    trials = json.loads(result.stdout)["trials"]
    require(
        len(trials) == 8 and all(trial["status"] == "ok" for trial in trials), trials
    )
    require(
        {trial["parameters"]["Mode"] for trial in trials} == {"fast", "slow"}, trials
    )
    require(
        {trial["parameters"]["Timeframe"] for trial in trials} == {"240", "1D"}, trials
    )
    (directory / "symbol-free-strings-result.json").write_bytes(result.stdout)
    print(
        "PASS symbol-free input.string/input.timeframe: real v1.2.0 artifact, "
        "Python + native, 8 trials, no input kinds",
        flush=True,
    )

    work["strategies"][0].pop("source")
    work["strategies"][0]["artifact"] = artifact["plugin"]
    spec.write_text(json.dumps(work))
    refusal = (
        "manifest not stamped kind-capable by pineforge-hpo's builder; "
        "rebuild the artifact with pineforge-hpo >= 0.8.0 and codegen >= 1.1.0"
    )
    manifest.pop("input_kind_schema")
    provenance_path = Path(artifact["provenance"])
    provenance = json.loads(provenance_path.read_text())
    provenance.pop("input_kind_schema")
    provenance_path.write_text(json.dumps(provenance))
    for unrelated_kind in (None, 42, "symbol", "string"):
        manifest["inputs"].append(
            {"title": "Unrelated", "type": "int", "kind": unrelated_kind}
        )
        manifest_path.write_text(json.dumps(manifest))
        try:
            prepare_run(
                spec,
                ROOT / "external/pineforge-engine",
                directory / "cache",
                native=args.native,
            )
        except CliError as error:
            require(refusal in str(error), str(error))
        else:
            raise AssertionError(
                "Python let an unrelated kind vouch for a string input"
            )
        refused = subprocess.run(command, capture_output=True, timeout=30)
        require(
            refused.returncode == 1
            and refusal.encode() in refused.stderr
            and json.loads(refused.stdout)["ok"] is False,
            refused.stderr.decode(),
        )
        manifest["inputs"].pop()
    print(
        "PASS unstamped real artifact: rebuild cause in both frontends; "
        "unrelated null/numeric/string/symbol kinds never vouch",
        flush=True,
    )
    manifest["input_kind_schema"] = 1
    provenance["input_kind_schema"] = 1
    provenance_path.write_text(json.dumps(provenance))
    mode = next(item for item in manifest["inputs"] if item["title"] == "Mode")
    mode["type"] = "foo"
    manifest_path.write_text(json.dumps(manifest))
    message = "search_space.Mode.choices[0] is incompatible with Pine input type 'foo'"
    try:
        prepare_run(
            spec,
            ROOT / "external/pineforge-engine",
            directory / "cache",
            native=args.native,
        )
    except CliError as error:
        require(str(error) == message, str(error))
    else:
        raise AssertionError("Python accepted an unknown manifest input type")
    refused = subprocess.run(command, capture_output=True, timeout=30)
    require(
        refused.returncode == 1
        and message.encode() in refused.stderr
        and json.loads(refused.stdout)["ok"] is False,
        refused.stderr.decode(),
    )
    print(
        "PASS unknown manifest type: Python/native error-text parity, before trials",
        flush=True,
    )


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
    input_metadata_probe(args, directory, document)
    symbol_free_string_probe(args, directory, document)
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
        and json.loads(refused.stdout)["ok"] is False
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
