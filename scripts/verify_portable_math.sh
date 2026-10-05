#!/usr/bin/env bash
set -euo pipefail
mode=${1:-release}
jobs=${2:-4}
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
export PYTHONPATH="$root/python:$root/external/pineforge-codegen-oss${PYTHONPATH:+:$PYTHONPATH}"
engine="$root/external/pineforge-engine"
dlib=${PFH_DLIB_SOURCE:-$root/build/_deps/dlib-src}
if [[ "$dlib" != /* ]]; then dlib="$root/$dlib"; fi
mkdir -p build/evidence
uname -a > build/evidence/host.txt
if command -v lscpu >/dev/null; then lscpu >> build/evidence/host.txt; fi
configure_engine() {
    cmake -S "$engine" -B "$engine/build" -DCMAKE_BUILD_TYPE=Release \
        -DPINEFORGE_BUILD_TESTS=OFF -DPINEFORGE_BUILD_TUTORIAL=OFF \
        -DPINEFORGE_BUILD_CORPUS_STRATEGIES=OFF -DPINEFORGE_BUILD_BENCH_STRATEGIES=OFF \
        -DPINEFORGE_BUILD_SPEED_BENCH=OFF
}
case "$mode" in
release)
    configure_engine
    cmake -S . -B build/release -DCMAKE_BUILD_TYPE=Release \
        -DPINEFORGE_ENGINE_ROOT="$engine" -DPINEFORGE_HPO_REQUIRE_SERIAL_GOLDEN=ON \
        -DFETCHCONTENT_SOURCE_DIR_DLIB="$dlib"
    cmake --build build/release -j"$jobs"
    ctest --test-dir build/release --output-on-failure
    build/release/bin/pineforge_hpo_portable_math_proof build/evidence/serial 1 | tee build/evidence/serial.log
    build/release/bin/pineforge_hpo_portable_math_proof build/evidence/threaded 8 | tee build/evidence/threaded.log
    diff -r build/evidence/serial build/evidence/threaded
    python3 -m unittest discover -s tests/python -v
    ;;
proof)
    configure_engine
    cmake -S . -B build/release -DCMAKE_BUILD_TYPE=Release \
        -DPINEFORGE_ENGINE_ROOT="$engine" -DPINEFORGE_HPO_REQUIRE_SERIAL_GOLDEN=ON \
        -DFETCHCONTENT_SOURCE_DIR_DLIB="$dlib"
    cmake --build build/release --target pineforge_hpo_portable_math_proof pineforge_hpo_test_portable_math -j"$jobs"
    build/release/bin/pineforge_hpo_test_portable_math
    build/release/bin/pineforge_hpo_portable_math_proof build/evidence/serial 1 | tee build/evidence/serial.log
    build/release/bin/pineforge_hpo_portable_math_proof build/evidence/threaded 8 | tee build/evidence/threaded.log
    diff -r build/evidence/serial build/evidence/threaded
    if [[ -d tests/fixtures/portable-math-intel ]]; then
        build/release/bin/pineforge_hpo_portable_math_proof build/evidence/intel-restored 8 \
            tests/fixtures/portable-math-intel | tee build/evidence/intel-restored.log
    fi
    ;;
performance)
    [[ -d build/baseline-source ]] || git worktree add --detach build/baseline-source 50a995c9c0c0ae04956442e9d2c604baf274b058
    python3 benchmarks/portable_math/run_performance.py --baseline "$root/build/baseline-source" \
        --output build/evidence/performance --dlib "$dlib" --jobs "$jobs"
    ;;
optuna)
    python3 -m venv build/optuna-venv
    build/optuna-venv/bin/pip install -r benchmarks/optuna/requirements.txt
    cmake -S benchmarks/optuna -B build/optuna -DCMAKE_BUILD_TYPE=Release \
        -DFETCHCONTENT_SOURCE_DIR_DLIB="$dlib"
    cmake --build build/optuna --target pineforge_hpo_optuna_native -j"$jobs"
    build/optuna-venv/bin/python benchmarks/optuna/run_benchmark.py \
        --native build/optuna/pineforge_hpo_optuna_native --smoke \
        --output build/evidence/optuna-smoke.csv
    ;;
sanitizers|asan|tsan)
    configure_engine
    presets="asan tsan"
    if [[ "$mode" != sanitizers ]]; then presets="$mode"; fi
    for preset in $presets; do
        cmake --preset "$preset" -DPINEFORGE_ENGINE_ROOT="$engine" \
            -DFETCHCONTENT_SOURCE_DIR_DLIB="$dlib"
        cmake --build --preset "$preset" -j"$jobs"
        ctest --preset "$preset"
    done
    ;;
docs)
    rm -rf build/docs
    mkdir -p build/docs
    PROJECT_NUMBER="$(tr -d '\n' < VERSION)" doxygen Doxyfile
    test ! -s build/docs/doxygen-warnings.log
    python3 docs/pages/validate_generated_site.py build/docs/html
    ;;
contract)
    configure_engine
    cmake --build "$engine/build" --target pineforge -j"$jobs"
    git worktree add --detach build/baseline-source 50a995c9c0c0ae04956442e9d2c604baf274b058
    cmake -S build/baseline-source -B build/baseline -DCMAKE_BUILD_TYPE=Release \
        -DPINEFORGE_HPO_BUILD_TESTS=OFF -DPINEFORGE_ENGINE_ROOT="$engine" \
        -DFETCHCONTENT_SOURCE_DIR_DLIB="$dlib"
    cmake --build build/baseline -j"$jobs"
    python3 tests/test_symbol_feeds_contract.py build/release/bin/pineforge-hpo-native \
        build/release/lib/fake_strategy.so --baseline build/baseline/bin/pineforge-hpo-native \
        --harness "$engine/docker/run_json.py"
    for arm in baseline portable; do
        source="$root"
        if [[ "$arm" == baseline ]]; then source="$root/build/baseline-source"; fi
        cmake -S benchmarks/portable_math -B "build/compat-$arm" \
            -DCMAKE_BUILD_TYPE=Release -DPINEFORGE_HPO_ROOT="$source" \
            -DFETCHCONTENT_SOURCE_DIR_DLIB="$dlib"
        cmake --build "build/compat-$arm" --target compatibility_probe -j"$jobs"
        "build/compat-$arm/compatibility_probe" > "build/evidence/compat-$arm.txt"
    done
    cmp build/evidence/compat-baseline.txt build/evidence/compat-portable.txt
    sha256sum build/evidence/compat-*.txt
    python3 tests/test_symbol_feeds_e2e.py --native build/release/bin/pineforge-hpo-native \
        --probe build/release/bin/pineforge_hpo_symbol_feed_report \
        --harness "$engine/docker/run_json.py" --output build/evidence/feeds-e2e
    ;;
*) echo "unknown mode: $mode" >&2; exit 2 ;;
esac
echo "PASS portable-math $mode"
