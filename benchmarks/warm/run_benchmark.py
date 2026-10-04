"""Reproducible warm-load resources, with an 8-GiB Linux cgroup per measured process."""

from __future__ import annotations

import argparse
from array import array
import csv
import hashlib
import json
from pathlib import Path
import platform
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))
from pineforge_hpo.continuation import recorded_space, space_hash  # noqa: E402
from pineforge_hpo.study_spec import load_study_spec  # noqa: E402
from pineforge_hpo.warm_binary import HEADER, MAGIC, VERSION  # noqa: E402

SEED = 73
COUNTS = (100_000, 500_000, 1_000_000, 2_000_000)
INPUTS = (5, 32)


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def value(row, column, indexed):
    if indexed:
        return (row * 500_009 + column * 73) % 2_000_003
    return ((row * 1_000_003 + column * 73) % (1 << 21)) / (1 << 21)


def generate(directory, count, inputs):
    label = f"n{count}-p{inputs}"
    spec_path = directory / (label + ".spec.json")
    spec = json.loads((ROOT / "examples/single_strategy/study.json").read_text())
    indexed = 4 if inputs == 32 else 0
    parameters = {f"P{column:02d}":
                  {"kind": "integer", "low": 0, "high": 2_000_002, "step": 1}
                  if column < indexed else {"kind": "real", "low": 0.0, "high": 1.0}
                  for column in range(inputs)}
    spec["strategies"][0].update(source="unused.pine", search_space=parameters)
    spec["objective"].update(expression="metrics.all.net_profit", constraints=[])
    spec["sampler"].update(kind="random", seed=SEED, trials=1)
    spec_path.write_text(json.dumps(spec))
    study = load_study_spec(spec_path)
    binary = directory / (label + ".bin")
    legacy = directory / (label + ".json")
    header_bytes = 80 + 4 * inputs
    row_bytes = 17 + 4 * indexed + 8 * (inputs - indexed)
    with binary.open("wb") as output:
        output.write(HEADER.pack(MAGIC, VERSION, 0, header_bytes,
                                 header_bytes + count * row_bytes, count, inputs, 1, 0, 0,
                                 bytes.fromhex(space_hash(study))))
        for column in range(inputs):
            output.write(struct.pack("<BBH", 1 if column < indexed else 2,
                                     1 if column < indexed else 2, 0))
        for begin in range(0, count, 65_536):
            output.write(array("Q", range(begin, min(count, begin + 65_536))).tobytes())
        output.write(b"\0" * count)
        for column in range(inputs):
            for begin in range(0, count, 65_536):
                output.write(array("i" if column < indexed else "d",
                                   (value(row, column, column < indexed)
                                    for row in range(begin, min(count, begin + 65_536))))
                             .tobytes())
        for begin in range(0, count, 65_536):
            output.write(array("d", (float(row % 997) for row in
                                      range(begin, min(count, begin + 65_536)))).tobytes())
    with legacy.open("w") as output:
        output.write('{"space":' + json.dumps(recorded_space(study), separators=(",", ":"))
                     + ',"trials":[')
        for row in range(count):
            trial = {"trial_id": row, "status": "ok", "feasible": True,
                     "parameters": {name: value(row, column, column < indexed)
                                    for column, name in enumerate(parameters)},
                     "objective": float(row % 997),
                     "metrics": {"all": {"net_profit": float(row % 997),
                                          "num_trades": row % 100,
                                          "max_drawdown": float(row % 17)}}}
            output.write(("," if row else "") + json.dumps(trial, separators=(",", ":")))
        output.write("]}\n")
    return spec_path, legacy, binary


def measure(native, spec, warm, result_path, cold):
    if cold:
        subprocess.run(["sudo", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"], check=True)
    command = ["sudo", "systemd-run", "--quiet", "--wait", "--pipe", "--collect",
               "-p", "MemoryMax=8G", "-p", "MemorySwapMax=0", "/usr/bin/time",
               "-f", "%e,%M,%x", "-o", str(result_path), str(native), "space-info",
               "--spec", str(spec), "--warm-start", str(warm)]
    process = subprocess.run(command, capture_output=True, text=True, timeout=240)
    timing = result_path.read_text().splitlines()[-1].split(",")
    elapsed, peak_rss = float(timing[0]), int(timing[1])
    if process.returncode == 0:
        info = json.loads(process.stdout)
        if info["tried"] != int(warm.stem.split("-")[0][1:]):
            raise AssertionError("benchmark dropped or duplicated parameter vectors")
    (result_path.with_suffix(".stderr")).write_text(process.stderr)
    return elapsed, peak_rss, process.returncode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-revision", help="tested source commit for copied build trees")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if sys.byteorder != "little" or platform.system() != "Linux":
        parser.error("resource driver requires little-endian Linux")
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    rows = []
    files = []
    started = time.time()
    output = directory / "resources.csv"
    fields = ("trials", "inputs", "format", "cache", "repeat", "file_bytes",
              "elapsed_seconds", "peak_rss_kib", "exit_code")
    with output.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for inputs in INPUTS:
            for count in COUNTS:
                spec, legacy, binary = generate(directory, count, inputs)
                for format_name, native, warm in (("v0.5-json", args.baseline, legacy),
                                                  ("v0.6-binary", args.native, binary)):
                    files.append({"name": warm.name, "bytes": warm.stat().st_size,
                                  "sha256": digest(warm)})
                    for repeat in range(args.repeats + 1):
                        cold = repeat == 0
                        timing = directory / f"{warm.stem}-{format_name}-{repeat}.time"
                        elapsed, rss, status = measure(native.resolve(), spec, warm, timing, cold)
                        row = dict(zip(fields, (count, inputs, format_name,
                                              "cold" if cold else "warm", repeat,
                                              warm.stat().st_size, elapsed, rss, status)))
                        writer.writerow(row)
                        stream.flush()
                        rows.append(row)
                        print(json.dumps(row), flush=True)
                legacy.unlink()
                binary.unlink()
    metadata = {"seed": SEED, "source_revision": args.source_revision,
                "baseline_revision": "df7f61300fbb9222f20022127ec0058cc0c9c8d1",
                "versions": ["0.5.0", "0.6.0"], "compiler_flags": "CMake Release (-O3 -DNDEBUG)",
                "cpu": "8 vCPU, x86_64", "physical_memory_gib": 16,
                "memory_limit_bytes": 8 << 30,
                "memory_scope": "per-process cgroup v2, MemorySwapMax=0",
                "measurement": "native space-info: mmap/read, validation, exact tried index; "
                               "excludes strategy/data loading and TPE fitting/replay; "
                               "binary payload SHA-256 deferred; original JSON loader includes it",
                "fixture": "seeded exact unique permuted parameters; 5 continuous, "
                           "32 mixed (4 int32 grids + 28 float64); feasible objective ties; "
                           "JSON retains three report metrics",
                "cold": "drop_caches before first run; subsequent repeats warm page cache",
                "binary_sha256": digest(args.native.resolve()),
                "baseline_binary_sha256": digest(args.baseline.resolve()),
                "files": files, "csv_sha256": digest(output),
                "duration_seconds": time.time() - started}
    output.with_suffix(".csv.metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(f"resources: {len(rows)} measurements, metadata and hashes retained", flush=True)


if __name__ == "__main__":
    main()
