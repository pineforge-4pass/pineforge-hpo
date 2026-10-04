#!/usr/bin/env python3
"""Publish path-free review-fix evidence and verified source/binary sidecars."""

import argparse
import csv
import hashlib
import json
from pathlib import Path
import platform
import subprocess


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--full-history-metadata", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    args.output.mkdir(parents=True, exist_ok=True)
    source_paths = [
        "VERSION",
        "CMakeLists.txt",
        "src/core/tpe_sampler.cpp",
        "src/core/ordinal_set.hpp",
        "src/cli/main.cpp",
        "include/pineforge/hpo/sampler.hpp",
        "src/cli/batch_executor.hpp",
        "tests/test_tpe_scale.cpp",
        "tests/test_trial_outputs.py",
    ]
    common = {
        "base_revision": "8db9840cbe96ea56db0984aa0110c6de65d43661",
        "repository_dirty": True,
        "selected_variant": "fresh",
        "release_gate": "blocked",
        "platform": {"system": platform.system(), "machine": platform.machine()},
        "compiler": subprocess.check_output(
            ["c++", "--version"], text=True
        ).splitlines()[0],
        "source_sha256": {path: digest(root / path) for path in source_paths},
        "normalizers_sha256": digest(args.evidence / "normalizers.json"),
        "coco_revision": "dd4bd1f0cc7699a2b612448d85aafb94636fe947",
        "engine_revision": "5718c5dc05086fc5b66b4cb565617efe837131e3",
        "codegen_revision": "5bf595b5e826562c131710b3e93e96e6f8e0a0e4",
    }

    def publish(kind, rows, metadata):
        path = args.output / f"2026-10-03-review-{kind}.csv"
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(
                stream, fieldnames=sorted({key for row in rows for key in row})
            )
            writer.writeheader()
            writer.writerows(rows)
        sidecar = {**common, **metadata, "rows": len(rows), "csv_sha256": digest(path)}
        path.with_suffix(".csv.metadata.json").write_text(
            json.dumps(sidecar, indent=2) + "\n"
        )
        if digest(path) != sidecar["csv_sha256"]:
            raise RuntimeError("CSV changed while sealing provenance")
        print(kind, len(rows), sidecar["csv_sha256"], flush=True)

    normalizers = json.loads((args.evidence / "normalizers.json").read_text())
    quality = []
    traces = {}
    binaries = {}
    references = {
        "fresh": "full-history-fresh",
        "reservoir448": "full-history",
        "final": "full-history-final",
        "weighted": "full-history-final",
        "fresh-weighted": "full-history-fresh",
    }
    for variant, long_reference in references.items():
        for budget in (3000, 10000):
            reference = "baseline" if budget == 3000 else long_reference
            metadata_path = args.evidence / "long" / f"{variant}-{budget}.metadata.json"
            run_metadata = json.loads(metadata_path.read_text())
            result_paths = [
                args.evidence / "long" / f"{variant}-{problem}-{seed}-{budget}.json"
                for problem in run_metadata["problems"]
                for seed in run_metadata["seeds"]
            ]
            for result_path in result_paths:
                result = json.loads(result_path.read_text())
                if "trace" not in result:
                    continue
                suffix = result_path.name[len(variant) + 1 :]
                reference_path = result_path.with_name(f"{reference}-{suffix}")
                before = json.loads(reference_path.read_text())
                problem, seed = suffix.rsplit("-", 2)[:2]
                scale = normalizers[f"{problem}:1"]["scale"]
                previous = (
                    max(0, before["trace"][budget - 1][1] - before["optimum"]) / scale
                )
                current = (
                    max(0, result["trace"][budget - 1][1] - result["optimum"]) / scale
                )
                quality.append(
                    {
                        "variant": variant,
                        "reference": reference,
                        "problem": problem,
                        "seed": int(seed),
                        "budget": budget,
                        "reference_regret": previous,
                        "review_regret": current,
                        "paired_ratio": current / max(previous, 1e-300),
                    }
                )
                for path in (result_path, reference_path):
                    traces[path.name] = digest(path)
                    label = variant if path == result_path else reference
                    metadata_path = path.with_name(f"{label}-{budget}.metadata.json")
                    if metadata_path.exists():
                        binaries[label] = json.loads(metadata_path.read_text())[
                            "binary_sha256"
                        ]
    publish(
        "quality",
        quality,
        {
            "binary_sha256": binaries,
            "trace_sha256": traces,
            "protocol": "pinned HPO-BENCH instance1 batch8/lag0, common frozen 0.3 coordinator",
            "full_history_reference": json.loads(
                args.full_history_metadata.read_text()
            ),
        },
    )
    asks = [
        json.loads(line)
        for line in (args.evidence / "fresh-ask.ndjson").read_text().splitlines()
    ]
    publish(
        "ask",
        asks,
        {
            "binary_sha256": digest(args.evidence / "fresh-ask-profile"),
            "protocol": "random sphere prefill then 256 asks with ordered batch8 updates; contended",
        },
    )
    native = []
    for directory in ("finite-fresh", "scale-fresh-million"):
        path = args.evidence / directory / "native-profiles.ndjson"
        if path.exists():
            native.extend(
                json.loads(line) for line in path.read_text().splitlines() if line
            )
    publish(
        "native",
        native,
        {"protocol": "W8 batch8 lag1, strict-parsed complete billing lines"},
    )
    ordinal_path = args.evidence / "ordinal-final.ndjson"
    if ordinal_path.exists():
        ordinals = [json.loads(line) for line in ordinal_path.read_text().splitlines()]
        publish(
            "ordinal",
            ordinals,
            {
                "binary_sha256": digest(args.evidence / "ordinal-final"),
                "protocol": "1M unique pseudo-random ordinals in a 100M-cardinality space",
            },
        )


if __name__ == "__main__":
    main()
