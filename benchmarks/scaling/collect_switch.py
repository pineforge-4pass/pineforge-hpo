#!/usr/bin/env python3
"""Publish path-free history-switch evidence and verify each CSV digest."""

import argparse
import csv
import hashlib
import json
from pathlib import Path
import platform
import statistics
import subprocess


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--measurements", type=Path, required=True)
    parser.add_argument("--normalizers", type=Path, required=True)
    parser.add_argument("--full-history-metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    args.output.mkdir(parents=True, exist_ok=True)
    sources = ["src/core/tpe_sampler.cpp", "src/core/pruner.cpp", "src/core/ordinal_set.hpp",
               "src/cli/main.cpp", "src/cli/batch_executor.hpp",
               "include/pineforge/hpo/sampler.hpp", "python/pineforge_hpo/study_spec.py",
               "python/pineforge_hpo/cli.py"]
    sources.extend(str(path.relative_to(root)) for path in
                   sorted((root / "benchmarks/scaling").glob("*.py")))
    sources.extend(str(path.relative_to(root)) for path in
                   sorted((root / "benchmarks/scaling").glob("*.cpp")))
    common = {"schema": "pineforge-hpo.scaling-evidence.v1", "version": "0.4.0",
              "baseline_revision": "6ccb6d4", "rebased_main_revision": "13885b9",
              "release_gate": "blocked", "history_switch": 8,
              "ask_budget_us_per_dimension": 375 / 64,
              "platform": {"system": platform.system(), "machine": platform.machine()},
              "compiler": subprocess.check_output(["g++", "--version"], text=True).splitlines()[0],
              "build": "Release, C++17, -O3 -DNDEBUG; AWS c6i.2xlarge, 8 vCPU",
              "source_sha256": {path: digest(root / path) for path in sources},
              "normalizers_sha256": digest(args.normalizers),
              "coco_repository": "numbbo/coco-experiment",
              "coco_revision": "dd4bd1f0cc7699a2b612448d85aafb94636fe947",
              "coco_source_verification": "52 pinned files exact; generated version comments differ",
              "engine_revision": "5718c5dc05086fc5b66b4cb565617efe837131e3",
              "codegen_revision": "5bf595b5e826562c131710b3e93e96e6f8e0a0e4"}

    def publish(name, rows, metadata):
        path = args.output / f"2026-10-03-switch-{name}.csv"
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=sorted({key for row in rows for key in row}))
            writer.writeheader()
            writer.writerows(rows)
        sidecar = {**common, **metadata, "rows": len(rows), "csv_sha256": digest(path)}
        path.with_suffix(".csv.metadata.json").write_text(json.dumps(sidecar, indent=2) + "\n")
        if digest(path) != sidecar["csv_sha256"]:
            raise RuntimeError("CSV changed while sealing provenance")
        print(name, len(rows), sidecar["csv_sha256"], flush=True)

    normalizers = json.loads(args.normalizers.read_text())
    quality = []
    traces = {}
    binaries = {}
    summaries = {}
    for budget, reference in ((3000, "baseline"), (10000, "full-switch8")):
        metadata = json.loads((args.evidence / "long" /
                               f"switch8-{budget}.metadata.json").read_text())
        for problem in metadata["problems"]:
            for seed in metadata["seeds"]:
                values = []
                for label in (reference, "switch8"):
                    path = args.evidence / "long" / f"{label}-{problem}-{seed}-{budget}.json"
                    result = json.loads(path.read_text())
                    values.append(max(0, result["trace"][budget - 1][1] - result["optimum"]) /
                                  normalizers[f"{problem}:1"]["scale"])
                    traces[path.name] = digest(path)
                    run_metadata = json.loads((path.parent /
                                               f"{label}-{budget}.metadata.json").read_text())
                    binaries[label] = run_metadata["binary_sha256"]
                quality.append({"problem": problem, "seed": seed, "budget": budget,
                                "reference": reference, "reference_regret": values[0],
                                "switch_regret": values[1],
                                "paired_ratio": values[1] / max(values[0], 1e-300)})
        summaries[str(budget)] = json.loads((args.evidence / "long" /
                                            f"switch8-{budget}.summary.json").read_text())
    publish("quality", quality, {"binary_sha256": binaries, "trace_sha256": traces,
            "protocol": "pinned HPO-BENCH instance1 batch8/lag0, frozen 0.3 coordinator",
            "baseline_3000": "reused verified round-one traces for the same pinned binary",
            "full_history_reference": json.loads(args.full_history_metadata.read_text()),
            "summaries": summaries})

    asks = []
    for label, filename in (("legacy-default", "legacy-default.ndjson"),
                            ("legacy-forced-fit", "legacy-forced-fit.ndjson"),
                            ("legacy-switch-cost", "legacy-switch.ndjson"),
                            ("switch8", "switch-ask.ndjson")):
        asks.extend({"sampler": label, **row} for row in
                    read_lines(args.measurements / filename))
    publish("ask", asks, {"protocol": "random prefill; legacy snapshot; bounded 256 batch8-updated asks",
            "limitations": "shared host; random-prefill probes are not adaptive million-trial studies"})
    native = []
    for directory in ("million", "hundred-thousand"):
        native.extend(read_lines(args.measurements / directory / "native-profiles.ndjson"))
    publish("native", native, {"protocol": "64D, W8 batch8 lag1, 3ms fixture, full strict billing parse"})
    publish("ordinal", read_lines(args.measurements / "ordinal.ndjson"),
            {"protocol": "1M unique pseudo-random ordinals in a 100M-cardinality space",
             "binary_sha256": digest(args.measurements / "ordinal-profile")})
    identity = json.loads((args.measurements / "identity" / "summary.json").read_text())
    publish("identity", identity["rows"], {key: value for key, value in identity.items()
                                          if key != "rows"})
    for budget, summary in summaries.items():
        print(f"QUALITY {budget}")
        for row in summary["rows"]:
            print(f"| {row['problem']} | {row['reference_regret']:.9g} | "
                  f"{row['after_regret']:.9g} | {row['ratio']:.6f} |")
        print(f"geomean={summary['geomean_ratio']:.6f}, worst={summary['worst_problem_ratio']:.6f}")
    print("ASK: dims,1k,10k,100k,1M")
    for dimensions in (8, 16, 32, 64):
        values = [statistics.median(row["ask_us"] for row in asks if row["sampler"] == "switch8"
                  and row["dims"] == dimensions and row["history"] == history)
                  for history in (1000, 10000, 100000, 1000000)]
        print(dimensions, *(f"{value:.3f}" for value in values))
    for row in native:
        print("NATIVE", row["trials_completed"], "rss_kib", row["peak_rss_kib"],
              "trials_per_second", row["trials_completed"] / row["elapsed_seconds"],
              "line_serialization_us", row["progress_serialization_seconds"] * 1e6 /
              row["trials_completed"], "line_write_us", row["progress_write_seconds"] * 1e6 /
              row["trials_completed"])


if __name__ == "__main__":
    main()
