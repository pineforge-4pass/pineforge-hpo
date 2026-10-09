"""Return-statistics build identity: stability, sensitivity, descriptor fidelity, TPE isolation.

Usage: test_return_stats_identity.py REPOSITORY_ROOT DLIB_SOURCE_DIR

Every scenario configures its own temporary projects; the repository itself is never written to.
The identity is a build binding. This test does not prove arithmetic, repeat or worker-count
invariance, and it does not replace the reference-value and reconciliation proofs.
"""

from __future__ import annotations

import collections
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CONTRACT = "pineforge-hpo-return-stats/v1"
IDENTITY_PREFIX = "pineforge-hpo-return-stats-build/v1:sha256:"
CONFIGURATION = "Release"
GENERATED = Path("generated/return_stats_identity") / CONFIGURATION
GENERATION_TARGET = "x_reducer_return_stats_identity"

REDUCER_HPP = (
    "#pragma once\n"
    "namespace fixture { double sum_squares(const double* values, int count); }\n"
)
REDUCER_CPP = (
    '#include "reducer.hpp"\n'
    "namespace fixture {\n"
    "double sum_squares(const double* values, int count) {\n"
    "    double total = 0.0;\n"
    "    for (int index = 0; index < count; ++index) total += values[index] * values[index];\n"
    "    return total;\n"
    "}\n"
    "}\n"
)
PROBE_CPP = (
    "#include <pineforge/hpo/return_stats_identity.hpp>\n"
    "#include <iostream>\n"
    "int main() {\n"
    "    using namespace pineforge::hpo;\n"
    '    std::cout << "identity=" << return_stats_numeric_build_identity() << "\\n"\n'
    '              << "source_digest=" << return_stats_source_digest() << "\\n"\n'
    '              << "contract=" << return_stats_contract() << "\\n"\n'
    '              << "descriptor_begin\\n" << return_stats_identity_descriptor()\n'
    '              << "descriptor_end\\n";\n'
    "    return 0;\n"
    "}\n"
)
FIXTURE_CMAKE = """\
cmake_minimum_required(VERSION 3.17)
project(PfhReturnStatsFixture C CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
include("@REPOSITORY@/cmake/ReturnStatsIdentity.cmake")
set(PFH_TEST_SOURCE_OPTIONS "-ffp-contract=off;-fno-fast-math" CACHE STRING "")
set(PFH_TEST_COMPILER_VERSION "" CACHE STRING "")
set(PFH_TEST_CONTRACT "pineforge-hpo-return-stats/v1" CACHE STRING "")
set(PFH_TEST_LIST_GENERATED OFF CACHE BOOL "")
add_library(x_reducer STATIC reducer.cpp)
add_executable(x_probe probe.cpp)
target_include_directories(x_probe PRIVATE "@REPOSITORY@/include")
set(extra_headers "")
if(PFH_TEST_LIST_GENERATED)
    set(extra_headers
        "${CMAKE_CURRENT_BINARY_DIR}/generated/return_stats_identity/Release/return_stats_identity_generated.hpp")
endif()
set(version_argument "")
if(PFH_TEST_COMPILER_VERSION)
    set(version_argument COMPILER_VERSION "${PFH_TEST_COMPILER_VERSION}")
endif()
pfh_return_stats_identity(
    TARGET x_reducer
    SOURCES reducer.cpp
    HEADERS reducer.hpp ${extra_headers}
    CONTRACT "${PFH_TEST_CONTRACT}"
    SOURCE_OPTIONS ${PFH_TEST_SOURCE_OPTIONS}
    CONSUMERS x_probe
    ${version_argument})
"""
# Appended to a copy of the repository's root CMakeLists.txt: X is added to the real core target.
TPE_INTEGRATION = """

include("${CMAKE_CURRENT_SOURCE_DIR}/cmake/ReturnStatsIdentity.cmake")
target_sources(pineforge_hpo_core PRIVATE x_fixture/reducer.cpp)
pfh_return_stats_identity(
    TARGET pineforge_hpo_core
    SOURCES x_fixture/reducer.cpp
    HEADERS x_fixture/reducer.hpp
    CONTRACT "pineforge-hpo-return-stats/v1"
    SOURCE_OPTIONS -ffp-contract=off -fno-fast-math
    CONSUMERS pineforge_hpo_core)
"""

SEPARATE_PATH_OPTIONS = {
    "-I", "-isystem", "-iquote", "-idirafter", "-iframework", "-F", "-include", "-imacros",
    "-isysroot", "--sysroot", "-MF", "-MT", "-MQ", "-o",
}
JOINED_PATH_PREFIXES = ("-I", "-F", "-MF", "-MT", "-MQ", "-o", "--sysroot=")
DROPPED_OPTIONS = {"-c", "-MD", "-MMD", "-MP", "-MG"}
INJECTED_PATTERNS = [
    re.compile(pattern)
    for pattern in (
        r"-std=(c|gnu)\+\+\w+",
        r"-fPIC",
        r"-fPIE",
        r"-fpic",
        r"-fpie",
        r"-pthread",
        r"-mmacosx-version-min=.+",
        r"--target=.+",
        r"-m32",
        r"-m64",
        r"-flto(=.*)?",
        r"-fvisibility=.+",
        r"-fvisibility-inlines-hidden",
    )
]
ARCHITECTURES = {"arm64", "arm64e", "x86_64", "i386"}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def run(command, *, cwd=None, timeout=300, check=True):
    words = [str(part) for part in command]
    process = subprocess.run(
        words, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )
    if check and process.returncode != 0:
        raise AssertionError(
            f"{' '.join(words)} exited with {process.returncode}\n"
            f"{process.stdout}\n{process.stderr}"
        )
    return process


def write_fixture(directory, repository):
    directory.mkdir(parents=True)
    cmake = FIXTURE_CMAKE.replace("@REPOSITORY@", repository.as_posix())
    (directory / "CMakeLists.txt").write_text(cmake)
    (directory / "reducer.hpp").write_text(REDUCER_HPP)
    (directory / "reducer.cpp").write_text(REDUCER_CPP)
    (directory / "probe.cpp").write_text(PROBE_CPP)


def configure(source, build, *definitions, check=True):
    return run(
        [
            "cmake",
            "-S",
            source,
            "-B",
            build,
            f"-DCMAKE_BUILD_TYPE={CONFIGURATION}",
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
            *definitions,
        ],
        check=check,
    )


def build_target(build, target):
    run(["cmake", "--build", build, "--target", target], timeout=600)


def identity_of(build):
    return (build / GENERATED / "return_stats_identity.txt").read_text().strip()


def descriptor_of(build):
    return (build / GENERATED / "return_stats_identity.descriptor.txt").read_bytes().decode(
        "utf-8"
    )


def generate_identity(source, build, *definitions):
    configure(source, build, *definitions)
    build_target(build, GENERATION_TARGET)
    return identity_of(build)


def changed_lines(first, second):
    return set(first.splitlines()) ^ set(second.splitlines())


def parse_descriptor(text):
    lines = text.split("\n")
    require(lines[-1] == "", "the descriptor must end with a newline")
    lines = lines[:-1]
    require(
        lines[0] == "pineforge-hpo-return-stats-identity/v1", "unexpected descriptor header"
    )
    parsed = {"scalars": {}, "flags": [], "defines": [], "sources": [], "headers": []}
    for line in lines[1:]:
        if line.startswith("flag "):
            _, origin, token = line.split(" ", 2)
            parsed["flags"].append((origin, token))
        elif line.startswith("define "):
            _, origin, token = line.split(" ", 2)
            parsed["defines"].append((origin, token))
        elif line.startswith("source "):
            name, digest = line[len("source "):].rsplit(" sha256=", 1)
            parsed["sources"].append((name, digest))
        elif line.startswith("header "):
            name, digest = line[len("header "):].rsplit(" sha256=", 1)
            parsed["headers"].append((name, digest))
        else:
            key, value = line.split("=", 1)
            parsed["scalars"][key] = value
    return parsed


def normalize_command(entry):
    """Drop everything that is not a numerically relevant flag of the translation unit."""
    arguments = entry["arguments"] if "arguments" in entry else shlex.split(entry["command"])
    directory = Path(entry["directory"])
    source = Path(entry["file"])
    if not source.is_absolute():
        source = directory / source
    kept = []
    skip_next = False
    for token in arguments[1:]:
        if skip_next:
            skip_next = False
            continue
        if token in SEPARATE_PATH_OPTIONS:
            skip_next = True
            continue
        if token in DROPPED_OPTIONS or token.startswith(JOINED_PATH_PREFIXES):
            continue
        if not token.startswith("-"):
            candidate = Path(token)
            if not candidate.is_absolute():
                candidate = directory / candidate
            if candidate.resolve() == source.resolve():
                continue
        kept.append(token)
    return kept


def is_injected(token, previous):
    if token == "-arch" or (previous == "-arch" and token in ARCHITECTURES):
        return True
    return any(pattern.fullmatch(token) for pattern in INJECTED_PATTERNS)


def match_flags(actual, expected):
    """The expected flags must be an ordered subsequence; extra tokens must be injected ones."""
    position = 0
    residual = []
    previous = ""
    for token in actual:
        if position < len(expected) and token == expected[position]:
            position += 1
        elif not is_injected(token, previous):
            residual.append(token)
        previous = token
    return position == len(expected), residual


def check_against_entry(parsed, entry):
    actual = normalize_command(entry)
    flags = [token for _, token in parsed["flags"]]
    actual_defines = collections.Counter(token for token in actual if token.startswith("-D"))
    expected_defines = collections.Counter(
        [token for token in flags if token.startswith("-D")]
        + [f"-D{token}" for _, token in parsed["defines"]]
    )
    require(
        actual_defines == expected_defines,
        f"definitions differ: command {dict(actual_defines)} descriptor {dict(expected_defines)}",
    )
    matched, residual = match_flags(
        [token for token in actual if not token.startswith("-D")],
        [token for token in flags if not token.startswith("-D")],
    )
    require(matched, f"descriptor flags are not an ordered part of the command: {actual}")
    require(not residual, f"command carries flags the descriptor does not bind: {residual}")


def compile_entry(build, source_name):
    database = json.loads((build / "compile_commands.json").read_text())
    entries = [entry for entry in database if Path(entry["file"]).name == source_name]
    require(len(entries) == 1, f"expected one compile command for {source_name}: {entries}")
    return entries[0]


def detected_compiler(build):
    files = sorted(build.glob("CMakeFiles/*/CMakeCXXCompiler.cmake"))
    require(files, "CMake's compiler detection file was not found")
    text = files[-1].read_text()
    identity = re.search(r'set\(CMAKE_CXX_COMPILER_ID "([^"]*)"\)', text)
    version = re.search(r'set\(CMAKE_CXX_COMPILER_VERSION "([^"]*)"\)', text)
    require(identity and version, "compiler detection file lacks identity or version")
    return identity.group(1), version.group(1)


def strip_comments(text, marker):
    return "\n".join(line.split(marker, 1)[0] for line in text.splitlines())


def scenario_text_hygiene(repository):
    header = (repository / "include/pineforge/hpo/return_stats_identity.hpp").read_text()
    code = re.sub(r"/\*.*?\*/", "", strip_comments(header, "//"), flags=re.S).lower()
    for forbidden in ("numeric_build", "tpe", "sampler", "checkpoint", "dlib"):
        require(forbidden not in code, f"the public header must not reference '{forbidden}'")
    helper = "\n".join(
        strip_comments((repository / "cmake" / name).read_text(), "#")
        for name in ("ReturnStatsIdentity.cmake", "GenerateReturnStatsIdentity.cmake")
    )
    for forbidden in (
        "target_compile_options",
        "target_compile_definitions",
        "add_compile_options",
        "add_definitions",
        "set_target_properties",
        "target_link_options",
        "set(CMAKE_CXX_FLAGS",
        "numeric_build_flags",
        "GenerateNumericBuildFlags",
    ):
        require(forbidden not in helper, f"the helper must not use '{forbidden}'")
    note = (repository / "docs/internal/return-stats-identity.md").read_text()
    private = re.search(
        r"(^|[\s`\"'(=])/(Users|home|private|tmp|var|opt|mnt|root)/", note, re.M
    )
    require(not private, "the note must not contain an absolute private path")
    require(repository.as_posix() not in note, "the note must not contain the repository path")
    print("PASS: public header and helper stay out of the TPE identity; note has no private path")


def scenario_stable(source, base):
    first_build = base / "stable-a"
    first = generate_identity(source, first_build)
    second = generate_identity(source, base / "stable-b")
    require(first.startswith(IDENTITY_PREFIX), f"unexpected identity format: {first}")
    require(first == second, "the identity depends on the build directory")
    require(
        descriptor_of(first_build) == descriptor_of(base / "stable-b"),
        "the descriptor depends on the build directory",
    )
    header = first_build / GENERATED / "return_stats_identity_generated.hpp"
    stamp = header.stat().st_mtime_ns
    build_target(first_build, GENERATION_TARGET)
    require(identity_of(first_build) == first, "an unchanged rebuild changed the identity")
    require(header.stat().st_mtime_ns == stamp, "an unchanged rebuild rewrote the header")
    moved = base / "moved-source"
    shutil.copytree(source, moved)
    require(
        generate_identity(moved, base / "stable-c") == first,
        "the identity depends on where the source tree lives",
    )
    for build in (first_build, base / "stable-b", base / "stable-c"):
        generated_header = build / GENERATED / "return_stats_identity_generated.hpp"
        text = descriptor_of(build) + generated_header.read_text()
        for location in (source, moved, build, base):
            require(location.as_posix() not in text, f"private path {location} leaked")
    print("PASS: unchanged builds, other build directories and moved trees keep the identity")


def scenario_sensitive(source, base):
    build = base / "sensitive"
    baseline = generate_identity(source, build)
    baseline_text = descriptor_of(build)

    for name, prefixes in (
        ("reducer.cpp", ("source ", "source.digest=")),
        ("reducer.hpp", ("header ", "source.digest=")),
    ):
        path = source / name
        original = path.read_text()
        path.write_text(original + "// edited\n")
        try:
            build_target(build, GENERATION_TARGET)
            edited = identity_of(build)
            edited_text = descriptor_of(build)
        finally:
            path.write_text(original)
        require(edited != baseline, f"editing {name} did not change the identity")
        require(
            all(line.startswith(prefixes) for line in changed_lines(baseline_text, edited_text)),
            f"editing {name} changed more than its own digest lines",
        )
        build_target(build, GENERATION_TARGET)
        require(identity_of(build) == baseline, f"restoring {name} did not restore the identity")

    probe = source / "probe.cpp"
    original = probe.read_text()
    probe.write_text(original + "// not a reducer file\n")
    try:
        build_target(build, GENERATION_TARGET)
        require(identity_of(build) == baseline, "an unlisted file changed the identity")
    finally:
        probe.write_text(original)

    variants = {
        "baseline": baseline,
        "extra-source-option": generate_identity(
            source,
            base / "variant-option",
            "-DPFH_TEST_SOURCE_OPTIONS=-ffp-contract=off;-fno-fast-math;-fno-builtin",
        ),
        "reordered-source-options": generate_identity(
            source,
            base / "variant-order",
            "-DPFH_TEST_SOURCE_OPTIONS=-fno-fast-math;-ffp-contract=off",
        ),
        "configuration-flags-O1": generate_identity(
            source, base / "variant-o1", "-DCMAKE_CXX_FLAGS_RELEASE=-O1"
        ),
        "configuration-flags-O2": generate_identity(
            source, base / "variant-o2", "-DCMAKE_CXX_FLAGS_RELEASE=-O2"
        ),
        "global-flags": generate_identity(
            source, base / "variant-global", "-DCMAKE_CXX_FLAGS=-fno-strict-aliasing"
        ),
        "compiler-version": generate_identity(
            source, base / "variant-version", "-DPFH_TEST_COMPILER_VERSION=0.0.0-test"
        ),
        "contract": generate_identity(
            source,
            base / "variant-contract",
            "-DPFH_TEST_CONTRACT=pineforge-hpo-return-stats/v2",
        ),
    }
    require(
        len(set(variants.values())) == len(variants),
        f"two differently bound builds share an identity: {variants}",
    )
    print(f"PASS: source, header, flag, order, compiler and contract changes move the identity "
          f"({len(variants)} distinct identities)")


def scenario_not_circular(source, base):
    process = configure(
        source, base / "circular", "-DPFH_TEST_LIST_GENERATED=ON", check=False
    )
    require(process.returncode != 0, "listing the generated header must fail the configure")
    require("circular" in process.stderr, f"unexpected refusal text: {process.stderr}")
    print("PASS: the generated header cannot be an input of its own identity")


def scenario_api(source, base):
    build = base / "api"
    configure(source, build)
    build_target(build, "x_probe")
    output = run([build / "x_probe"]).stdout
    identity = re.search(r"^identity=(.*)$", output, re.M).group(1)
    digest = re.search(r"^source_digest=(.*)$", output, re.M).group(1)
    contract = re.search(r"^contract=(.*)$", output, re.M).group(1)
    descriptor = output.split("descriptor_begin\n", 1)[1].rsplit("descriptor_end\n", 1)[0]
    require(contract == CONTRACT, f"unexpected contract {contract}")
    require(descriptor == descriptor_of(build), "the compiled descriptor differs from the file")
    require(identity == identity_of(build), "the compiled identity differs from the file")
    digest_of_descriptor = hashlib.sha256(descriptor.encode("utf-8")).hexdigest()
    require(identity == IDENTITY_PREFIX + digest_of_descriptor, "identity is not the digest")
    require(
        parse_descriptor(descriptor)["scalars"]["source.digest"] == digest,
        "the source digest differs from the descriptor",
    )
    print("PASS: the compiled interface returns the retained descriptor and its digest")


def scenario_descriptor_matches_commands(source, base):
    build = base / "commands"
    generate_identity(source, build)
    parsed = parse_descriptor(descriptor_of(build))
    entry = compile_entry(build, "reducer.cpp")
    check_against_entry(parsed, entry)
    actual = normalize_command(entry)
    for flag in ("-ffp-contract=off", "-fno-fast-math"):
        require(flag in actual, f"{flag} is missing from the real command: {actual}")

    compiler_id, compiler_version = detected_compiler(build)
    require(parsed["scalars"]["compiler.id"] == compiler_id, "compiler id differs from CMake")
    require(
        parsed["scalars"]["compiler.version"] == compiler_version,
        "compiler version differs from CMake",
    )
    banner = parsed["scalars"]["compiler.banner"]
    if banner != "unavailable":
        words = entry["arguments"] if "arguments" in entry else shlex.split(entry["command"])
        compiler = words[0]
        first_line = run([compiler, "--version"]).stdout.splitlines()[0].replace(";", ",")
        require(banner == first_line, f"banner '{banner}' differs from '{first_line}'")
    require(parsed["scalars"]["configuration"] == CONFIGURATION, "wrong configuration bound")

    # Negative controls: the same check must refuse a descriptor that does not match.
    without_contraction = [item for item in parsed["flags"] if item[1] != "-ffp-contract=off"]
    for label, tampered in (
        ("extra flag", dict(parsed, flags=parsed["flags"] + [("source", "-fno-such-flag-pfh")])),
        ("missing flag", dict(parsed, flags=without_contraction)),
        ("extra definition", dict(parsed, defines=parsed["defines"] + [("source", "PFH_EXTRA")])),
    ):
        try:
            check_against_entry(tampered, entry)
        except AssertionError:
            continue
        raise AssertionError(f"the command check accepted a descriptor with an {label}")
    print("PASS: the descriptor matches the real compile command and refuses tampered copies")


def scenario_tpe_unchanged(repository, dlib, base):
    definitions = (
        f"-DCMAKE_BUILD_TYPE={CONFIGURATION}",
        "-DPINEFORGE_HPO_BUILD_TESTS=OFF",
        "-DPINEFORGE_HPO_BUILD_NATIVE_CLI=OFF",
        "-DPINEFORGE_HPO_BUILD_ENGINE_ADAPTER=OFF",
        f"-DFETCHCONTENT_SOURCE_DIR_DLIB={dlib}",
    )

    def copy_tree(destination):
        destination.mkdir(parents=True)
        for name in ("CMakeLists.txt", "VERSION", "cmake", "include", "src", "third_party"):
            origin = repository / name
            if origin.is_dir():
                shutil.copytree(origin, destination / name)
            else:
                shutil.copy2(origin, destination / name)

    baseline_source, extended_source = base / "tpe-baseline", base / "tpe-with-x"
    copy_tree(baseline_source)
    copy_tree(extended_source)
    (extended_source / "x_fixture").mkdir()
    (extended_source / "x_fixture/reducer.hpp").write_text(REDUCER_HPP)
    (extended_source / "x_fixture/reducer.cpp").write_text(REDUCER_CPP)
    with open(extended_source / "CMakeLists.txt", "a") as handle:
        handle.write(TPE_INTEGRATION)

    builds = {}
    for label, source in (("baseline", baseline_source), ("with-x", extended_source)):
        build = base / f"tpe-build-{label}"
        run(["cmake", "-S", source, "-B", build, *definitions], timeout=600)
        build_target(build, "pineforge_hpo_numeric_flags")
        builds[label] = build
    build_target(builds["with-x"], "pineforge_hpo_core_return_stats_identity")

    def tpe_files(build):
        directory = build / "generated" / CONFIGURATION / "CXX"
        return tuple(
            (directory / name).read_bytes()
            for name in ("numeric_build_flags.txt", "numeric_build_flags.hpp")
        )

    reference = tpe_files(builds["baseline"])
    require(tpe_files(builds["with-x"]) == reference, "adding X changed the TPE flag identity")
    require(
        not (builds["baseline"] / "generated/return_stats_identity").exists(),
        "the baseline unexpectedly carries X outputs",
    )
    outputs = builds["with-x"] / GENERATED
    for name in (
        "return_stats_identity.descriptor.txt",
        "return_stats_identity.txt",
        "return_stats_identity_generated.hpp",
    ):
        require((outputs / name).is_file(), f"X output {name} was not generated")
    before = identity_of(builds["with-x"])

    reducer = extended_source / "x_fixture/reducer.cpp"
    reducer.write_text(reducer.read_text() + "// edited\n")
    build_target(builds["with-x"], "pineforge_hpo_numeric_flags")
    build_target(builds["with-x"], "pineforge_hpo_core_return_stats_identity")
    require(identity_of(builds["with-x"]) != before, "editing the reducer did not move X")
    require(tpe_files(builds["with-x"]) == reference, "an X source edit changed the TPE identity")
    print("PASS: TPE flag identity is byte-identical with X added and after an X source edit")


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    repository, dlib = (Path(argument).resolve() for argument in sys.argv[1:3])
    scenario_text_hygiene(repository)
    with tempfile.TemporaryDirectory(prefix="pfh-rsi-") as temporary:
        base = Path(temporary)
        fixture = base / "fixture"
        write_fixture(fixture, repository)
        scenario_stable(fixture, base)
        scenario_sensitive(fixture, base)
        scenario_not_circular(fixture, base)
        scenario_api(fixture, base)
        scenario_descriptor_matches_commands(fixture, base)
        scenario_tpe_unchanged(repository, dlib, base)
    print("PASS: return-statistics build identity")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
