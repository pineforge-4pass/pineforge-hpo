#!/usr/bin/env python3
"""Black-box regression tests for native CLI initialization and result semantics."""

from __future__ import annotations

import csv as csv_module
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import struct
import sys
import tempfile


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def invoke(
    native: Path,
    plugin: Path,
    csv: Path,
    *extra: str,
    sampler: str = "grid",
    max_trials: int = 1,
    workers: int = 1,
    length_low: int = 14,
    length_high: int = 14,
    ohlcv_option: str = "--ohlcv",
) -> subprocess.CompletedProcess[str]:
    command = [
        str(native),
        "run",
        "--strategy",
        str(plugin),
        ohlcv_option,
        str(csv),
        "--objective",
        "metrics.all.net_profit",
        "--sampler",
        sampler,
        "--max-trials",
        str(max_trials),
        "--workers",
        str(workers),
        "--input-tf",
        "1",
        "--script-tf",
        "5",
        "--chart-timezone",
        "Asia/Taipei",
        "--bar-magnifier",
        "true",
        "--magnifier-samples",
        "6",
        "--magnifier-distribution",
        "triangle",
        "--int-dim",
        "Length",
        str(length_low),
        str(length_high),
        "1",
        *extra,
    ]
    return subprocess.run(command, text=True, capture_output=True, check=False)


def test_binary_chart(native: Path, plugin: Path, csv: Path) -> None:
    binary = csv.with_suffix(".bin")
    with csv.open(newline="", encoding="utf-8") as source:
        rows = list(csv_module.DictReader(source))
    binary.write_bytes(
        b"".join(
            struct.pack(
                "<5dq",
                *(float(row[key]) for key in ("open", "high", "low", "close", "volume")),
                int(row["timestamp"]),
            )
            for row in rows
        )
    )
    sidecar = Path(str(binary) + ".json")
    metadata = {
        "format": "pf_bar_t/v1", "record_size": 48, "symbol": "TEST:BIN", "tf": "1",
        "count": len(rows), "first_ts": int(rows[0]["timestamp"]),
        "last_ts": int(rows[-1]["timestamp"]),
        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    }
    sidecar.write_text(json.dumps(metadata), encoding="utf-8")
    # The fixture plugin normally ignores chart prices. This opt-in mode scores the actual
    # chart fields; the changed-input run below proves these comparisons observe the bars.
    study = ("--seed", "170905", "--fixed-input", "ChartBarsTest", "1")
    csv_trials = csv.parent / "csv-trials.jsonl"
    bin_trials = csv.parent / "bin-trials.jsonl"
    baseline = invoke(
        native, plugin, csv, *study, "--trials-file", str(csv_trials),
        max_trials=3, length_low=2, length_high=4,
    )
    mapped = invoke(
        native, plugin, binary, *study, "--trials-file", str(bin_trials),
        max_trials=3, length_low=2, length_high=4, ohlcv_option="--ohlcv-bin",
    )
    require(baseline.returncode == 0, f"binary test CSV study failed: {baseline.stderr}")
    require(mapped.returncode == 0, f"binary study failed: {mapped.stderr}")
    csv_result = json.loads(baseline.stdout)
    bin_result = json.loads(mapped.stdout)
    require(len(bin_result["trials"]) == 3, "binary study did not execute three trials")
    require(
        all(trial["status"] == "ok" for trial in bin_result["trials"]),
        "binary study contains a failed trial",
    )
    # Trial JSONL contains no wall-clock or path fields: compare the complete original bytes,
    # without masking or reserializing any record.
    require(csv_trials.read_bytes() == bin_trials.read_bytes(), "binary/CSV trial bytes differ")
    require(csv_result["trials"] == bin_result["trials"], "binary/CSV trial values differ")

    changed_rows = list(rows)
    changed_rows[0] = {**changed_rows[0], "close": "100.5"}
    changed = csv.parent / "changed.bin"
    changed.write_bytes(
        b"".join(
            struct.pack(
                "<5dq",
                *(float(row[key]) for key in ("open", "high", "low", "close", "volume")),
                int(row["timestamp"]),
            )
            for row in changed_rows
        )
    )
    Path(str(changed) + ".json").write_text(
        json.dumps({**metadata, "sha256": hashlib.sha256(changed.read_bytes()).hexdigest()}),
        encoding="utf-8",
    )
    different = invoke(native, plugin, changed, *study, ohlcv_option="--ohlcv-bin")
    original = invoke(native, plugin, binary, *study, ohlcv_option="--ohlcv-bin")
    require(different.returncode == original.returncode == 0, "chart sensitivity study failed")
    require(
        json.loads(different.stdout)["trials"][0]["objective"]
        != json.loads(original.stdout)["trials"][0]["objective"],
        "binary study ignored the changed chart bar",
    )

    feeds = csv.parent / "feeds.json"
    feeds.write_text(
        json.dumps({"symbols": {"TEST:CSV": {"feeds": {"1": csv.name}}}}),
        encoding="utf-8",
    )
    feed_study = (*study, "--symbol-feeds", str(feeds), "--script-tf", "1")
    csv_feed = invoke(native, plugin, csv, *feed_study)
    bin_feed = invoke(native, plugin, binary, *feed_study, ohlcv_option="--ohlcv-bin")
    require(csv_feed.returncode == 0, f"CSV symbol-feed study failed: {csv_feed.stderr}")
    require(bin_feed.returncode == 0, f"binary chart / CSV feed failed: {bin_feed.stderr}")
    require(
        json.loads(csv_feed.stdout)["trials"] == json.loads(bin_feed.stdout)["trials"],
        "binary chart changed CSV symbol-feed trial results",
    )
    require(
        json.loads(bin_feed.stdout)["trials"][0]["objective"]
        != json.loads(original.stdout)["trials"][0]["objective"],
        "binary-chart study did not consume the CSV symbol feed",
    )

    def refusal(process: subprocess.CompletedProcess[str], cause: str) -> None:
        require(process.returncode == 1, f"expected initialization exit 1: {process.stderr}")
        require(cause in process.stderr, f"missing named refusal {cause!r}: {process.stderr}")
        require(json.loads(process.stdout)["failure"]["exit_code"] == 1, "wrong failure JSON")

    truncated = csv.parent / "truncated.bin"
    truncated.write_bytes(binary.read_bytes()[:-1])
    refusal(
        invoke(native, plugin, truncated, ohlcv_option="--ohlcv-bin"),
        "size is not a multiple of 48",
    )
    sidecar.write_text(json.dumps({**metadata, "count": len(rows) + 1}), encoding="utf-8")
    refusal(
        invoke(native, plugin, binary, ohlcv_option="--ohlcv-bin"),
        "sidecar does not match the bar/close files",
    )
    refusal(
        invoke(native, plugin, csv, "--ohlcv-bin", str(binary)),
        "--ohlcv and --ohlcv-bin are mutually exclusive",
    )
    for csv_path, bin_path in (("", str(binary)), (str(csv), "")):
        refusal(
            subprocess.run(
                [str(native), "run", "--strategy", str(plugin), "--ohlcv", csv_path,
                 "--ohlcv-bin", bin_path, "--objective", "metrics.all.net_profit"],
                text=True, capture_output=True, check=False,
            ),
            "--ohlcv and --ohlcv-bin are mutually exclusive",
        )
    refusal(
        subprocess.run(
            [str(native), "run", "--strategy", str(plugin),
             "--objective", "metrics.all.net_profit"],
            text=True, capture_output=True, check=False,
        ),
        "one of --ohlcv or --ohlcv-bin is required",
    )
    print("binary native CLI: 3-trial byte identity, chart sensitivity, CSV feeds, 6 refusals")


def test_recorded_space_bound(native: Path, plugin: Path, csv: Path) -> None:
    def dimensions(count: int) -> list[str]:
        return [
            argument
            for index in range(count)
            for argument in ("--real-dim", f"x{index}", "-5", "5", "continuous")
        ]

    def recorded_space_bytes(stdout: str) -> int:
        marker = '"space": '
        start = stdout.index(marker) + len(marker)
        _, end = json.JSONDecoder().raw_decode(stdout, start)
        return len(stdout[start:end].encode("utf-8"))

    measured = invoke(
        native,
        plugin,
        csv,
        *dimensions(64),
        "--trials-out",
        "none",
        max_trials=1,
        sampler="random",
    )
    require(measured.returncode == 0, f"large-space control failed: {measured.stderr}")
    measured_json = json.loads(measured.stdout)
    space_bytes = recorded_space_bytes(measured.stdout)
    require(space_bytes > 1, "recorded-space control was unexpectedly empty")
    require(
        measured_json["trials_requested"] == measured_json["trials_completed"] == 1,
        "recorded-space control did not execute its requested row",
    )

    exact = invoke(
        native,
        plugin,
        csv,
        *dimensions(64),
        "--max-recorded-space-bytes",
        str(space_bytes),
        "--trials-out",
        "none",
        max_trials=1,
        sampler="random",
    )
    require(exact.returncode == 0, "recorded-space bound rejected an exact fit")
    require(exact.stdout == measured.stdout, "recorded-space bound changed result bytes")
    metadata = invoke(
        native, plugin, csv, *dimensions(64),
        "--max-recorded-space-bytes", str(space_bytes),
        "--artifact-key", "unrecorded metadata" * 1000,
        max_trials=1, sampler="random",
    )
    require(metadata.returncode == 0, "recorded-space bound counted unrelated input bytes")

    output = csv.parent / "refusal-output.json"
    output.write_bytes(b"keep this result")
    refused = invoke(
        native,
        plugin,
        csv,
        *dimensions(64),
        "--max-recorded-space-bytes",
        str(space_bytes - 1),
        "--output",
        str(output),
        max_trials=1,
        sampler="random",
    )
    require(refused.returncode == 1, "recorded-space overflow did not fail")
    refusal = json.loads(refused.stdout)
    require(
        refusal["failure"]
        == {
            "origin": "hpo",
            "code": "hpo_recorded_space_too_large",
            "args": {"actual_bytes": space_bytes, "max_bytes": space_bytes - 1},
            "exit_code": 1,
        },
        "recorded-space overflow was not typed from the emitted space",
    )
    require(output.read_bytes() == b"keep this result", "refusal touched --output")
    require("trials" not in refusal, "recorded-space refusal published a result")

    unlimited = invoke(
        native,
        plugin,
        csv,
        *dimensions(200),
        "--max-recorded-space-bytes",
        "unlimited",
        "--trials-out",
        "none",
        max_trials=1,
        sampler="random",
    )
    require(unlimited.returncode == 0, "explicit unlimited recorded-space bound failed")
    unlimited_result = json.loads(unlimited.stdout)
    require(
        unlimited_result["trials_requested"] == unlimited_result["trials_completed"] == 1,
        "explicit unlimited run ignored its requested trial budget",
    )
    require(
        set(unlimited_result["space"]["parameters"])
        == {"Length"} | {f"x{index}" for index in range(200)},
        "large-space control did not record every supplied dimension",
    )
    for value in (None, "0"):
        extra = () if value is None else ("--max-recorded-space-bytes", value)
        automatic = invoke(
            native, plugin, csv, *dimensions(200), *extra, "--trials-out", "none",
            max_trials=1, sampler="random",
        )
        require(
            automatic.returncode == 0,
            f"automatic bound {value} rejected a larger space: {automatic.stderr}",
        )
        require(
            automatic.stdout == unlimited.stdout,
            "automatic and unlimited large-space bytes differ",
        )

    # Feed metadata is added after recorded_space(). It belongs to the emitted byte count,
    # even when compact row metadata or final retention suppresses individual row spaces.
    feeds = csv.parent / "bound-feeds.json"
    feeds.write_text(
        json.dumps({"symbols": {"TEST:BOUND": {"feeds": {"1": str(csv)}}}}),
        encoding="utf-8",
    )
    feed_flags = ("--symbol-feeds", str(feeds), "--script-tf", "1")
    feed_control = invoke(native, plugin, csv, *feed_flags)
    require(feed_control.returncode == 0, "bound feed control failed")
    feed_bytes = recorded_space_bytes(feed_control.stdout)
    require(
        "symbol_feeds" in json.loads(feed_control.stdout)["space"],
        "feed control did not record feed metadata",
    )
    rows = csv.parent / "bound-trials.jsonl"
    rows.write_bytes(b"keep these rows")
    for mode, retention in (("full", "all"), ("hash", "best-k"), ("none", "none")):
        missing_output = csv.parent / f"refused-{mode}.json"
        refused = invoke(
            native, plugin, csv, *feed_flags,
            "--max-recorded-space-bytes", str(feed_bytes - 1),
            "--row-space", mode, "--trials-out", retention,
            "--trials-file", str(rows), "--output", str(missing_output),
        )
        require(refused.returncode == 1, "compact output bypassed the space byte bound")
        require(
            json.loads(refused.stdout)["failure"]["args"]
            == {"actual_bytes": feed_bytes, "max_bytes": feed_bytes - 1},
            "recorded-space count omitted emitted feed metadata",
        )
        require(
            not missing_output.exists() and rows.read_bytes() == b"keep these rows",
            "bound refusal opened an output or trial destination",
        )

    # Vary symbol/dimension names, domains, escaped choices and feed facts. Measure the
    # raw emitted descriptor: these inputs have no fixed per-symbol or per-dimension size.
    with csv.open(newline="", encoding="utf-8") as source:
        close_sum = sum(float(row["close"]) for row in csv_module.DictReader(source))
    feed_trials = 3
    for symbol_count in (46, 200):
        symbols = {
            f'TEST:BOUND{index}{"資" * (index % 7)}"\\': {
                "feeds": {"1": str(csv)},
                "syminfo": {
                    "currency": f'CUR{index}資"\\',
                    "mintick": (index + 1) / 1000,
                },
            }
            for index in range(symbol_count)
        }
        many_feeds = csv.parent / f"bound-many-feeds-{symbol_count}.json"
        many_feeds.write_text(json.dumps({"symbols": symbols}), encoding="utf-8")
        real_name = f'scale_{symbol_count}_"資\\'
        category_name = "Payload"
        choice = f'choice_{symbol_count}_"資\\\n'
        constraint = "metrics.all.net_profit > 0"
        many_flags = (
            "--symbol-feeds", str(many_feeds), "--script-tf", "1",
            "--real-dim", real_name, str(-symbol_count), str(symbol_count), str(symbol_count),
            "--categorical-choice", category_name, choice,
            "--constraint", constraint,
        )
        many_default = invoke(
            native, plugin, csv, *many_flags, max_trials=feed_trials, length_high=16,
        )
        require(
            many_default.returncode == 0,
            f"default rejected {symbol_count} symbol feeds: {many_default.stderr}",
        )
        many_result = json.loads(many_default.stdout)
        many_bytes = recorded_space_bytes(many_default.stdout)
        require(
            many_result["trials_requested"] == many_result["trials_completed"] == feed_trials
            and len(many_result["trials"]) == feed_trials
            and all(row["status"] == "ok" for row in many_result["trials"]),
            "default feed run did not execute its requested trials",
        )
        recorded_symbols = many_result["space"]["symbol_feeds"]["symbols"]
        require(
            set(recorded_symbols) == set(symbols),
            "default feed run did not record every supplied symbol",
        )
        require(
            all(
                recorded_symbols[name]["facts"] == definition["syminfo"]
                and set(recorded_symbols[name]["feeds"]) == set(definition["feeds"])
                for name, definition in symbols.items()
            ),
            "default feed run changed the supplied feed facts or timeframes",
        )
        parameters = many_result["space"]["parameters"]
        require(
            set(parameters) == {"Length", real_name, category_name}
            and parameters[real_name]
            == {
                "kind": "real", "low": -symbol_count, "high": symbol_count,
                "step": symbol_count, "log": False,
            }
            and parameters[category_name] == {"kind": "categorical", "choices": [choice]}
            and many_result["space"]["objective"]["constraints"] == [constraint]
            and all(
                row["parameters"][category_name] == choice for row in many_result["trials"]
            ),
            "feed control changed the supplied dimensions, escaped choices or constraints",
        )
        require(many_bytes > feed_bytes, "recorded space ignored the additional symbol feeds")
        require(
            many_result["trials"][0]["objective"]
            - json.loads(feed_control.stdout)["trials"][0]["objective"]
            == (len(symbols) - 1) * close_sum,
            "default feed run did not compute from all supplied CSV feeds",
        )
        for value in ("0", "unlimited", str(many_bytes)):
            accepted = invoke(
                native, plugin, csv, *many_flags, "--max-recorded-space-bytes", value,
                max_trials=feed_trials, length_high=16,
            )
            require(
                accepted.returncode == 0,
                f"bound {value} rejected {symbol_count} symbol feeds: {accepted.stderr}",
            )
            require(
                accepted.stdout == many_default.stdout,
                "automatic, unlimited and exact-fit symbol-feed bytes differ",
            )
        many_refused = invoke(
            native, plugin, csv, *many_flags, "--max-recorded-space-bytes", str(many_bytes - 1),
            "--output", str(output), "--trials-file", str(rows),
            max_trials=feed_trials, length_high=16,
        )
        require(many_refused.returncode == 1, "explicit bound admitted oversized feed metadata")
        require(
            json.loads(many_refused.stdout)["failure"]
            == {
                "origin": "hpo",
                "code": "hpo_recorded_space_too_large",
                "args": {"actual_bytes": many_bytes, "max_bytes": many_bytes - 1},
                "exit_code": 1,
            },
            "explicit feed bound lost the typed refusal or actual emitted byte count",
        )
        require(
            output.read_bytes() == b"keep this result" and rows.read_bytes() == b"keep these rows",
            "explicit feed bound touched an output or trial file",
        )

    # Two parameters and many long choices exercise text bytes without any symbol feeds.
    # Automatic admissions are controls; the input-derived operator bound tests refusal.
    choices = [f'choice_{index}_' + '資"\\\n' * (160 + index) for index in range(24)]
    text_flags = tuple(
        argument
        for choice in choices
        for argument in ("--categorical-choice", "Payload", choice)
    )
    text_trials = len(choices)
    text_default = invoke(native, plugin, csv, *text_flags, max_trials=text_trials)
    require(
        text_default.returncode == 0,
        f"default rejected the text-heavy study: {text_default.stderr}",
    )
    text_result = json.loads(text_default.stdout)
    text_bytes = recorded_space_bytes(text_default.stdout)
    require(
        set(text_result["space"]["parameters"]) == {"Length", "Payload"}
        and text_result["space"]["parameters"]["Payload"]
        == {"kind": "categorical", "choices": choices}
        and "symbol_feeds" not in text_result["space"],
        "text-heavy control did not record the supplied categorical choices",
    )
    require(
        text_result["trials_requested"] == text_result["trials_completed"] == text_trials
        and len(text_result["trials"]) == text_trials
        and all(row["status"] == "ok" for row in text_result["trials"])
        and {row["parameters"]["Payload"] for row in text_result["trials"]} == set(choices),
        "text-heavy control did not execute its requested categorical trials",
    )
    for value in ("0", "unlimited", str(text_bytes)):
        accepted = invoke(
            native, plugin, csv, *text_flags, "--max-recorded-space-bytes", value,
            max_trials=text_trials,
        )
        require(
            accepted.returncode == 0,
            f"bound {value} rejected the text-heavy study: {accepted.stderr}",
        )
        require(
            accepted.stdout == text_default.stdout,
            "automatic, unlimited and exact-fit text-heavy bytes differ",
        )
    text_output_mtime = output.stat().st_mtime_ns
    text_refused = invoke(
        native, plugin, csv, *text_flags, "--max-recorded-space-bytes", str(text_bytes - 1),
        "--output", str(output), "--trials-file", str(rows),
        max_trials=text_trials,
    )
    require(text_refused.returncode == 1, "explicit bound admitted oversized categorical text")
    require(
        json.loads(text_refused.stdout)["failure"]
        == {
            "origin": "hpo",
            "code": "hpo_recorded_space_too_large",
            "args": {"actual_bytes": text_bytes, "max_bytes": text_bytes - 1},
            "exit_code": 1,
        },
        "text-heavy bound lost the typed refusal or raw emitted byte count",
    )
    require(
        output.read_bytes() == b"keep this result"
        and output.stat().st_mtime_ns == text_output_mtime
        and rows.read_bytes() == b"keep these rows",
        "explicit text-heavy bound touched an output or trial file",
    )

    for value in ("-1", "+1", "1.5", " 1", "1 ", "1e3", "١", "", str(1 << 64)):
        invalid = invoke(native, plugin, csv, "--max-recorded-space-bytes", value)
        require(invalid.returncode == 1, f"invalid recorded-space bound accepted: {value!r}")
        require(
            json.loads(invalid.stdout)["failure"]["code"] == "hpo_cli_usage",
            "invalid recorded-space bound was not a typed usage failure",
        )


def test_recorded_space_bound_wrapper(native: Path, plugin: Path, csv: Path) -> None:
    # Run the real wrapper and native runner. The fixture's input changes both the emitted
    # space and parameter rows; no replacement process or constant completion hides forwarding.
    fixture = csv.parent / "bound-wrapper"
    fixture.mkdir()
    copied_plugin = fixture / plugin.name
    shutil.copyfile(plugin, copied_plugin)
    payload = '資"\\\n' * 1200
    (fixture / "manifest.json").write_text(
        json.dumps({
            "schema_version": 1,
            "input_kind_schema": 1,
            "artifact_key": "a" * 64,
            "plugin_sha256": hashlib.sha256(copied_plugin.read_bytes()).hexdigest(),
            "inputs": [
                {"title": "Length", "type": "int"},
                {"title": "Payload", "type": "string", "kind": "string"},
            ],
        }),
        encoding="utf-8",
    )
    spec = fixture / "study.json"
    spec.write_text(
        json.dumps({
            "schema_version": 1,
            "mode": "single_strategy",
            "strategies": [{
                "id": "fixture", "artifact": str(copied_plugin), "datasets": ["bars"],
                "search_space": {
                    "Length": {"kind": "integer", "low": 14, "high": 16, "step": 1},
                    "Payload": {"kind": "categorical", "choices": [payload]},
                },
            }],
            "datasets": [{
                "id": "bars", "ohlcv": str(csv), "input_tf": "1", "script_tf": "5",
                "chart_timezone": "Asia/Taipei",
            }],
            "objective": {
                "kind": "expression", "direction": "maximize",
                "expression": "metrics.all.net_profit",
            },
            "sampler": {"kind": "grid", "seed": 0, "trials": 3},
            "execution": {"workers": 1, "isolation": "threads"},
        }),
        encoding="utf-8",
    )
    control = invoke(
        native, copied_plugin, csv, "--categorical-choice", "Payload", payload,
        "--max-recorded-space-bytes", "unlimited",
        max_trials=3, length_high=16,
    )
    require(control.returncode == 0, f"escaped-space control failed: {control.stderr}")
    start = control.stdout.index('"space": ') + len('"space": ')
    _, end = json.JSONDecoder().raw_decode(control.stdout, start)
    space_bytes = len(control.stdout[start:end].encode("utf-8"))
    require(
        space_bytes > len(payload.encode("utf-8")),
        "wrapper control did not record the escaped payload bytes",
    )
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "python"),
    }
    # StudySpec has no magnifier flags. This exec-only adapter adds the existing fixture's
    # required backtest settings, forwarding every wrapper argument to the real native runner.
    runner = fixture / "native-with-fixture-settings"
    runner.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"os.execv({str(native)!r}, [{str(native)!r}, *sys.argv[1:], "
        "'--bar-magnifier', 'true', '--magnifier-samples', '6', "
        "'--magnifier-distribution', 'triangle'])\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    command = [
        sys.executable, "-m", "pineforge_hpo.cli", "run", str(spec),
        "--native", str(runner), "--result-chunk-bytes", "7", "--diagnostic-bytes", "256",
    ]
    output = fixture / "result.json"
    output.write_bytes(b"keep this result")
    before = output.stat()
    refused = subprocess.run(
        [*command, "--max-recorded-space-bytes", str(space_bytes - 1), "--output", str(output)],
        capture_output=True, text=True, env=environment, check=False,
    )
    require(refused.returncode == 1, "wrapper lost the recorded-space refusal")
    require(
        json.loads(refused.stdout) == {
            "schema_version": 1, "ok": False,
            "failure": {
                "origin": "hpo", "code": "hpo_recorded_space_too_large",
                "args": {
                    "actual_bytes": space_bytes,
                    "max_bytes": space_bytes - 1,
                },
                "exit_code": 1,
            },
        },
        f"wrapper did not preserve recorded-space failure metadata: "
        f"{refused.stdout} {refused.stderr}",
    )
    require(
        output.read_bytes() == b"keep this result"
        and output.stat().st_mtime_ns == before.st_mtime_ns,
        "wrapper refusal touched --output",
    )
    for value in (None, "0", str(space_bytes), "unlimited", str((1 << 64) - 1)):
        extra = [] if value is None else ["--max-recorded-space-bytes", value]
        accepted = subprocess.run(
            [*command, *extra],
            capture_output=True, text=True, env=environment, check=False,
        )
        require(
            accepted.returncode == 0,
            f"wrapper bound {value} failed ({accepted.returncode}): "
            f"{accepted.stderr} {accepted.stdout[:1200]}",
        )
        result = json.loads(accepted.stdout)
        require(
            result["trials_requested"] == result["trials_completed"] == len(result["trials"]) == 3,
            "wrapper result lost requested or completed rows",
        )
        require(
            [row["parameters"]["Length"] for row in result["trials"]] == [14, 15, 16]
            and all(row["parameters"]["Payload"] == payload for row in result["trials"]),
            "wrapper control ignored its study inputs",
        )
        require(
            len({row["objective"] for row in result["trials"]}) == 3,
            "wrapper control did not compute from the changed Length input",
        )
        require(
            result["space"] == json.loads(control.stdout)["space"],
            "wrapper bound changed the recorded space",
        )


def main() -> int:
    native = Path(sys.argv[1]).resolve()
    plugin = Path(sys.argv[2]).resolve()
    with tempfile.TemporaryDirectory() as directory:
        csv = Path(directory) / "bars.csv"
        csv.write_text(
            "timestamp,open,high,low,close,volume\n"
            "1700000000000,100,102,99,101,10\n"
            "1700000060000,101,103,100,102,11\n",
            encoding="utf-8",
        )

        test_binary_chart(native, plugin, csv)
        test_recorded_space_bound(native, plugin, csv)
        test_recorded_space_bound_wrapper(native, plugin, csv)

        unknown = invoke(
            native,
            plugin,
            csv,
            "--objective",
            "metrics.all.not_a_metric",
        )
        require(unknown.returncode == 1, "unknown metric did not fail initialization")
        require(
            json.loads(unknown.stdout)["failure"]["code"] == "hpo_study_spec_invalid",
            "unknown metric lacks a stable initialization code",
        )
        require(
            "unknown report metric" in unknown.stderr,
            "unknown metric diagnostic was not preserved",
        )

        renamed = invoke(
            native,
            plugin,
            csv,
            "--objective",
            "metrics.equity.sharpe_monthly",
            "--constraint",
            "metrics.equity.sharpe_tv == metrics.equity.sharpe_monthly",
            "--constraint",
            "metrics.equity.sortino_tv == metrics.equity.sortino_monthly",
            "--constraint",
            "metrics.equity.sortino_monthly != metrics.equity.sharpe_monthly",
        )
        require(
            renamed.returncode == 0,
            f"engine 1.0 metric names were rejected: {renamed.stderr}",
        )
        renamed_trial = json.loads(renamed.stdout)["trials"][0]
        require(
            all(
                renamed_trial[key] is None
                for key in ("failure_code", "failure_args", "failure_origin")
            ),
            "successful trial carries failure metadata",
        )
        require(
            renamed_trial["status"] == "ok" and renamed_trial["feasible"],
            "pre-1.0 metric aliases disagreed with the engine 1.0 names",
        )
        # The fake plugin reports score / 10 and score / 20 with score 1114.
        renamed_metrics = renamed_trial["metrics"]
        require(
            abs(renamed_metrics["metrics.equity.sharpe_monthly"] - 111.4) < 1e-9
            and abs(renamed_metrics["metrics.equity.sortino_monthly"] - 55.7) < 1e-9,
            "engine 1.0 metric names resolved to the wrong report fields",
        )

        legacy = invoke(
            native,
            plugin,
            csv,
            "--objective",
            "metrics.equity.sharpe_tv",
        )
        require(
            legacy.returncode == 0,
            f"pre-1.0 metric alias was rejected: {legacy.stderr}",
        )
        legacy_trial = json.loads(legacy.stdout)["trials"][0]
        require(
            legacy_trial["objective"] == renamed_trial["objective"],
            "pre-1.0 metric alias changed the objective value",
        )
        require(
            list(legacy_trial["metrics"]) == ["metrics.equity.sharpe_tv"],
            "result did not keep the metric name the objective used",
        )

        constraint = invoke(
            native,
            plugin,
            csv,
            "--constraint",
            "1 / 0",
            "--division-by-zero",
            "ieee",
            "--non-finite",
            "allow",
        )
        require(
            constraint.returncode == 2, "non-finite constraint selected a best trial"
        )
        constraint_json = json.loads(constraint.stdout)
        require(
            not constraint_json["ok"], "result without a feasible trial reported ok"
        )
        constraint_trial = constraint_json["trials"][0]
        require(not constraint_trial["feasible"], "non-finite constraint was feasible")
        require(
            constraint_trial["status"] == "constraint_error",
            "non-finite constraint had the wrong status",
        )

        compared_constraint = invoke(
            native,
            plugin,
            csv,
            "--constraint",
            "1 / 0 > 0",
            "--division-by-zero",
            "ieee",
            "--non-finite",
            "allow",
        )
        require(
            compared_constraint.returncode == 2,
            "comparison hid a non-finite constraint operand",
        )
        compared_trial = json.loads(compared_constraint.stdout)["trials"][0]
        require(
            compared_trial["status"] == "constraint_error"
            and not compared_trial["feasible"],
            "non-finite constraint operand became feasible",
        )

        objective = invoke(
            native,
            plugin,
            csv,
            "--objective",
            "1 / 0",
            "--division-by-zero",
            "ieee",
            "--non-finite",
            "allow",
        )
        require(objective.returncode == 2, "non-finite objective selected a best trial")
        objective_trial = json.loads(objective.stdout)["trials"][0]
        require(not objective_trial["feasible"], "non-finite objective was feasible")
        require(
            objective_trial["status"] == "objective_error",
            "non-finite objective had the wrong status",
        )

        typed = invoke(
            native,
            plugin,
            csv,
            "--categorical-int-choice",
            "Category",
            "7",
        )
        require(typed.returncode == 0, "typed categorical trial failed")
        typed_trial = json.loads(typed.stdout)["trials"][0]
        require(
            typed_trial["parameters"]["Category"] == 7,
            "integer categorical choice lost its type",
        )
        require(
            typed_trial["backtest"]
            == {
                "input_bars_processed": 2,
                "script_bars_processed": 1,
                "input_tf_seconds": 60,
                "script_tf_seconds": 300,
                "script_tf_ratio": 5,
                "needs_aggregation": True,
                "magnifier_sample_ticks_total": 72,
            },
            "native result omitted the engine timeframe aggregation evidence",
        )

        adaptive = invoke(
            native,
            plugin,
            csv,
            sampler="dlib_global",
            max_trials=8,
            workers=2,
            length_low=1,
            length_high=20,
        )
        require(adaptive.returncode == 0, "native dlib_global study failed")
        adaptive_json = json.loads(adaptive.stdout)
        require(adaptive_json["sampler"] == "dlib_global", "wrong sampler in output")
        require(adaptive_json["trials_completed"] == 8, "adaptive trial budget drifted")
        adaptive_trials = adaptive_json["trials"]
        require(
            adaptive_json["best_value"]
            == max(trial["objective"] for trial in adaptive_trials),
            "adaptive runner selected the wrong best trial",
        )
        repeat = invoke(
            native,
            plugin,
            csv,
            sampler="dlib_global",
            max_trials=8,
            workers=2,
            length_low=1,
            length_high=20,
        )
        require(repeat.returncode == 0, "repeated native dlib_global study failed")
        repeat_trials = json.loads(repeat.stdout)["trials"]
        require(
            [trial["parameters"] for trial in adaptive_trials]
            == [trial["parameters"] for trial in repeat_trials],
            "fixed seed and batch size did not reproduce dlib proposals",
        )

        tpe_args = (
            "--tpe-history-switch",
            "128",
            "--tpe-startup-trials",
            "3",
            "--tpe-ei-candidates",
            "16",
            "--tpe-gamma-fraction",
            "0.25",
            "--tpe-gamma-cap",
            "4",
            "--tpe-prior-weight",
            "1.5",
            "--tpe-constant-liar",
            "true",
        )
        tpe = invoke(
            native,
            plugin,
            csv,
            *tpe_args,
            sampler="tpe",
            max_trials=12,
            workers=3,
            length_low=1,
            length_high=20,
        )
        require(tpe.returncode == 0, f"native TPE study failed: {tpe.stderr}")
        tpe_json = json.loads(tpe.stdout)
        require(tpe_json["schema_version"] == 1, "wrong native result schema")
        require(
            tpe_json["pineforge_hpo_version"]
            == (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip(),
            "native result HPO version differs from VERSION",
        )
        require(
            tpe_json["sampler_implementation"] == "pineforge_product_tpe_v3_bounded",
            "native result omitted the TPE implementation version",
        )
        require(tpe_json["sampler"] == "tpe", "wrong TPE sampler in output")
        require(tpe_json["trials_completed"] == 12, "TPE trial budget drifted")
        require(
            tpe_json["candidate_policy"] == "sampler_default"
            and tpe_json["candidate_policy_implementation"] == "sampler_default",
            "default TPE candidate policy provenance drifted",
        )
        require(
            tpe_json["search_space_finite"]
            and tpe_json["search_space_cardinality"] == 20,
            "default TPE omitted finite search-space provenance",
        )
        require(
            tpe_json["trials_requested"] == 12
            and tpe_json["duplicate_proposals_skipped"] == 0,
            "default TPE trial or duplicate provenance drifted",
        )
        require(
            tpe_json["sampler_config"]
            == {
                "startup_trials": 3,
                "ei_candidates": 16,
                "scale_ei_candidates": 8,
                "history_switch": 128,
                "bad_reservoir_size": 448,
                "max_threads": 0,
                "gamma_fraction": 0.25,
                "gamma_cap": 4,
                "prior_weight": 1.5,
                "constant_liar": True,
            },
            "TPE configuration was not preserved in result provenance",
        )
        tpe_trials = tpe_json["trials"]
        require(
            tpe_json["tpe_history_switch"] == 128
            and all(trial["tpe_history_switch"] == 128 for trial in tpe_trials),
            "TPE history switch is missing from the result or trial archive",
        )
        unique_tpe_parameters = {
            tuple(sorted(trial["parameters"].items())) for trial in tpe_trials
        }
        require(
            tpe_json["unique_candidates_attempted"] == len(unique_tpe_parameters),
            "default TPE unique-candidate count disagreed with its trial table",
        )
        require(
            tpe_json["remaining_candidates"] == 20 - len(unique_tpe_parameters),
            "default TPE remaining-candidate count drifted",
        )
        require(
            not tpe_json["search_space_exhausted"]
            and tpe_json["stop_reason"] == "trial_budget_reached"
            and not tpe_json["full_parameter_coverage"]
            and not tpe_json["exhaustive_equivalent"],
            "default TPE overstated finite-space coverage",
        )
        require(
            tpe_json["best_value"] == max(trial["objective"] for trial in tpe_trials),
            "TPE runner selected the wrong best trial",
        )
        tpe_repeat = invoke(
            native,
            plugin,
            csv,
            *tpe_args,
            sampler="tpe",
            max_trials=12,
            workers=3,
            length_low=1,
            length_high=20,
        )
        require(tpe_repeat.returncode == 0, "repeated native TPE study failed")
        tpe_default = invoke(native, plugin, csv, sampler="tpe", max_trials=12)
        require(tpe_default.returncode == 0, "default full-history TPE study failed")
        tpe_default_json = json.loads(tpe_default.stdout)
        require(
            tpe_default_json["tpe_history_switch"] is None
            and tpe_default_json["sampler_config"]["history_switch"] is None
            and all(
                trial["tpe_history_switch"] is None
                for trial in tpe_default_json["trials"]
            ),
            "unset TPE history switch must be recorded as null",
        )
        tpe_repeat_trials = json.loads(tpe_repeat.stdout)["trials"]
        require(
            [trial["parameters"] for trial in tpe_trials]
            == [trial["parameters"] for trial in tpe_repeat_trials],
            "fixed seed and batch size did not reproduce TPE proposals",
        )

        exhaustive_dimensions = (
            "--candidate-policy",
            "exhaustive",
            "--bool-dim",
            "Enabled",
            "--bool-dim",
            "Confirmed",
        )
        exhaustive_tpe = invoke(
            native,
            plugin,
            csv,
            *exhaustive_dimensions,
            sampler="tpe",
            max_trials=4,
            workers=4,
        )
        require(
            exhaustive_tpe.returncode == 0,
            f"exhaustive 2x2 TPE study failed: {exhaustive_tpe.stderr}",
        )
        exhaustive_tpe_json = json.loads(exhaustive_tpe.stdout)
        exhaustive_tpe_parameters = {
            tuple(sorted(trial["parameters"].items()))
            for trial in exhaustive_tpe_json["trials"]
        }
        require(
            len(exhaustive_tpe_json["trials"]) == 4
            and len(exhaustive_tpe_parameters) == 4,
            "exhaustive 2x2 TPE did not attempt four unique parameter vectors",
        )
        require(
            exhaustive_tpe_json["sampler_implementation"]
            == "pineforge_product_tpe_v3_bounded_finite"
            and exhaustive_tpe_json["candidate_policy"] == "exhaustive"
            and exhaustive_tpe_json["candidate_policy_implementation"]
            == "pineforge_finite_space_v1",
            "exhaustive TPE implementation provenance drifted",
        )
        require(
            exhaustive_tpe_json["search_space_finite"]
            and exhaustive_tpe_json["search_space_cardinality"] == 4
            and exhaustive_tpe_json["trials_requested"] == 4
            and exhaustive_tpe_json["trials_completed"] == 4
            and exhaustive_tpe_json["unique_candidates_attempted"] == 4
            and exhaustive_tpe_json["remaining_candidates"] == 0,
            "exhaustive TPE finite-space counters drifted",
        )
        require(
            exhaustive_tpe_json["search_space_exhausted"]
            and exhaustive_tpe_json["stop_reason"] == "search_space_exhausted"
            and exhaustive_tpe_json["full_parameter_coverage"]
            and exhaustive_tpe_json["exhaustive_equivalent"],
            "exhaustive TPE did not certify complete successful coverage",
        )

        exhaustive_grid = invoke(
            native,
            plugin,
            csv,
            *exhaustive_dimensions,
            sampler="grid",
            max_trials=4,
            workers=4,
        )
        require(
            exhaustive_grid.returncode == 0,
            f"exhaustive 2x2 grid study failed: {exhaustive_grid.stderr}",
        )
        exhaustive_grid_json = json.loads(exhaustive_grid.stdout)
        exhaustive_grid_parameters = {
            tuple(sorted(trial["parameters"].items()))
            for trial in exhaustive_grid_json["trials"]
        }
        require(
            exhaustive_grid_json["sampler_implementation"] == "pineforge_grid_v2_finite"
            and exhaustive_grid_json["candidate_policy"] == "exhaustive"
            and exhaustive_grid_json["exhaustive_equivalent"],
            "exhaustive grid provenance drifted",
        )
        require(
            exhaustive_tpe_parameters == exhaustive_grid_parameters,
            "exhaustive TPE and grid covered different parameter sets",
        )

        missing_plugin = Path(directory) / "does-not-exist.dylib"
        missing_csv = Path(directory) / "does-not-exist.csv"
        continuous_exhaustive = invoke(
            native,
            missing_plugin,
            missing_csv,
            "--candidate-policy",
            "exhaustive",
            "--real-dim",
            "Continuous",
            "0",
            "1",
            "continuous",
            sampler="tpe",
            max_trials=2,
        )
        require(
            continuous_exhaustive.returncode == 1,
            "continuous exhaustive space reached nonexistent plugin/data paths",
        )
        require(
            "finite candidate policy requires a step on every varying real dimension"
            in continuous_exhaustive.stderr,
            "continuous exhaustive space did not fail at policy validation",
        )

        mismatched_exhaustive = invoke(
            native,
            missing_plugin,
            missing_csv,
            "--candidate-policy",
            "exhaustive",
            "--bool-dim",
            "Enabled",
            "--bool-dim",
            "Confirmed",
            sampler="tpe",
            max_trials=3,
        )
        require(
            mismatched_exhaustive.returncode == 1,
            "exhaustive cardinality mismatch reached nonexistent plugin/data paths",
        )
        require(
            "exhaustive candidate budget must equal search-space cardinality"
            in mismatched_exhaustive.stderr,
            "exhaustive cardinality mismatch did not fail before artifact loading",
        )

        invalid_tpe = invoke(
            native,
            plugin,
            csv,
            "--tpe-startup-trials",
            "0",
            sampler="tpe",
            max_trials=4,
        )
        require(invalid_tpe.returncode == 1, "invalid TPE config was accepted")
        require(
            "--tpe-startup-trials must be greater than zero" in invalid_tpe.stderr,
            "invalid TPE config diagnostic was not preserved",
        )

        oversized_tpe = invoke(
            native,
            plugin,
            csv,
            "--tpe-ei-candidates",
            "1000001",
            sampler="tpe",
            max_trials=4,
        )
        require(oversized_tpe.returncode == 1, "oversized TPE EI pool was accepted")
        require(
            "--tpe-ei-candidates must be between 1 and 1000000" in oversized_tpe.stderr,
            "oversized TPE EI diagnostic was not preserved",
        )

        log_tpe = invoke(
            native,
            plugin,
            csv,
            *tpe_args,
            "--log-int-dim",
            "Scale",
            "1",
            "1000000",
            "--log-real-dim",
            "Learning Rate",
            "0.000001",
            "1000",
            "--seed",
            "123",
            sampler="tpe",
            max_trials=64,
        )
        require(
            log_tpe.returncode == 0, f"native log TPE study failed: {log_tpe.stderr}"
        )
        log_trials = json.loads(log_tpe.stdout)["trials"]
        require(len(log_trials) == 64, "native log study trial budget drifted")
        require(
            all(
                1 <= trial["parameters"]["Scale"] <= 1_000_000
                and 1e-6 <= trial["parameters"]["Learning Rate"] <= 1e3
                for trial in log_trials
            ),
            "native log flags produced an out-of-bounds value",
        )

        invalid_log = invoke(
            native,
            plugin,
            csv,
            "--log-real-dim",
            "Learning Rate",
            "0",
            "1",
            sampler="random",
            max_trials=2,
        )
        require(invalid_log.returncode == 1, "non-positive log bound was accepted")
        require(
            "log real dimension bounds must be positive" in invalid_log.stderr,
            "invalid log-bound diagnostic was not preserved",
        )

    print("native CLI tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
