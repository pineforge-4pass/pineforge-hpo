#!/usr/bin/env python3
"""Materialize a benchmark-only full-history reference without changing production code."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("reference destination must not exist")
    shutil.copytree(args.source, args.output,
                    ignore=shutil.ignore_patterns(".git", "build", "build-*", "__pycache__"))
    path = args.output / "src/core/tpe_sampler.cpp"
    source = path.read_text()
    start = source.index("    void compact_observations(const TpeSamplerConfig& config) {")
    end = source.index("    bool register_pending(", start)
    replacement = """    void compact_observations(const TpeSamplerConfig& config) {
        const auto warmup_limit = std::max<std::uint64_t>(1000, config.gamma_cap + 64);
        if (history_.size() >= warmup_limit ||
            generated_.load(std::memory_order_relaxed) >= warmup_limit)
            compact_history_ = true;
    }

"""
    path.write_text(source[:start] + replacement + source[end:])
    metadata = {"purpose": "benchmark-only full-history 0.4.0 reference",
                "window": "unbounded", "model_refit_completions": "inherited from source",
                "post_warmup_ei_draws": 8, "numeric_density_points": 513,
                "original_sampler_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "reference_sampler_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "replacement": replacement}
    (args.output / "reference.metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")


if __name__ == "__main__":
    main()
