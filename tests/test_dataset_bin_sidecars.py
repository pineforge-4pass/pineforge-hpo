#!/usr/bin/env python3
"""Drive sidecar refusals through the real CLI, including against the parent loader."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from generate_dataset_bin_fixtures import SIDECAR_REFUSALS


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("native", type=Path)
    parser.add_argument("plugin", type=Path)
    parser.add_argument("fixtures", type=Path)
    parser.add_argument("--modes", action="store_true")
    args = parser.parse_args()
    failures = 0
    passed = 0

    def invoke(name: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(args.native.resolve()), "run", "--strategy", str(args.plugin.resolve()),
             "--ohlcv-bin", str(args.fixtures / name), "--objective", "metrics.all.net_profit",
             "--sampler", "grid", "--seed", "170905", "--workers", "1", "--max-trials", "1",
             "--fixed-input", "ChartBarsTest", "1", *extra],
            text=True, capture_output=True, timeout=10, check=False,
        )

    def check(label: str, name: str, cause: str | None, *extra: str) -> None:
        nonlocal failures, passed
        try:
            result = invoke(name, *extra)
            document = json.loads(result.stdout)
            if cause is None:
                assert result.returncode == 0, result.stderr
                assert document["trials"][0]["status"] == "ok", result.stdout
                evidence = "exit=0 status=ok"
            else:
                if result.returncode == 0:
                    raise AssertionError("accepted invalid sidecar (exit=0 status=" +
                                         document["trials"][0]["status"] + ")")
                assert result.returncode == 1, f"exit={result.returncode}: {result.stderr}"
                assert document["failure"]["exit_code"] == 1, result.stdout
                assert document["failure"]["code"] == "hpo_dataset_invalid", result.stdout
                assert cause in result.stderr, result.stderr
                evidence = "exit=1 code=hpo_dataset_invalid cause=" + cause
            print(f"PASS {label}: {evidence}", flush=True)
            passed += 1
        except (AssertionError, KeyError, ValueError, subprocess.TimeoutExpired) as error:
            print(f"FAIL {label}: {error}", flush=True)
            failures += 1

    good = args.fixtures / "good.bin"
    corrupt = args.fixtures / "digest-mismatch.bin"
    assert good.read_bytes() != corrupt.read_bytes(), "digest fixture did not change bar bytes"
    assert (args.fixtures / "good.bin.json").read_bytes() == Path(str(corrupt) + ".json").read_bytes()
    recorded = json.loads(Path(str(corrupt) + ".json").read_text())["sha256"]
    assert recorded == hashlib.sha256(good.read_bytes()).hexdigest()
    assert recorded != hashlib.sha256(corrupt.read_bytes()).hexdigest()
    check("valid-converter-metadata", "good.bin", None)
    mismatch = "sidecar does not match the bar/close files"
    for name in SIDECAR_REFUSALS:
        check(name, name, mismatch)
    if args.modes:
        check("full-explicit", "digest-mismatch.bin", mismatch, "--verify-bin", "full")
        check("sidecar-skips-digest", "digest-mismatch.bin", None, "--verify-bin", "sidecar")
        for name in SIDECAR_REFUSALS:
            if name != "digest-mismatch.bin":
                check("sidecar-" + name, name, mismatch, "--verify-bin", "sidecar")
        for name, cause in (("nonfinite-open.bin", "non-finite OHLC"),
                            ("timestamp-duplicate.bin", "timestamps must be strictly increasing"),
                            ("fifo-sidecar.bin", "sidecar unreadable"),
                            ("unreadable-sidecar.bin", "sidecar unreadable")):
            check(name, name, cause, "--verify-bin", "sidecar")
        invalid = invoke("good.bin", "--verify-bin", "unchecked")
        if (invalid.returncode == 1 and
                json.loads(invalid.stdout)["failure"]["code"] == "hpo_cli_usage" and
                "--verify-bin must be full or sidecar" in invalid.stderr):
            print("PASS invalid-verification-mode: exit=1 code=hpo_cli_usage", flush=True)
            passed += 1
        else:
            print(f"FAIL invalid-verification-mode: {invalid.stdout} {invalid.stderr}")
            failures += 1
    print(f"sidecar CLI tests: passed={passed} failed={failures}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
