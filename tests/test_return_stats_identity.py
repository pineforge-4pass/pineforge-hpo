"""Return-statistics build identity: bound to the generated compile commands, fail-closed otherwise.

Usage: test_return_stats_identity.py REPOSITORY_ROOT DLIB_SOURCE_DIR [--list] [--only TOKEN[,TOKEN...]]

Without options every scenario runs, in the same order as before. `--list` prints the scenario
tokens of this host (one per line, nothing is run); `--only` runs just the named tokens, in plan
order, and ends with PASS-PARTIAL instead of PASS.

The identity is derived from the compile command that the build system generated for each reducer
translation unit. This test keeps its own implementation of the declared normalization rule and
compares it with the generator's output, both on synthetic compilation databases (the generator
is a pure function of its inputs) and on the databases of configured projects, among them a copy
of the real core target. It checks the build binding only. It does not prove arithmetic, repeat
or worker-count invariance, and it does not replace the reference-value and reconciliation
proofs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CONTRACT = "pineforge-hpo-return-stats/v1"
FORMAT = "pineforge-hpo-return-stats-identity/v2"
IDENTITY_PREFIX = "pineforge-hpo-return-stats-build/v2:sha256:"
CONFIGURATION = "Release"
GENERATED = Path("generated/return_stats_identity")
# The root build file delimits its return-statistics block with these comment lines, so that the
# real-core scenario can cut it out to build its X-free baseline.
MARK_BEGIN = "# PFH-RETURN-STATS-IDENTITY-BEGIN"
MARK_END = "# PFH-RETURN-STATS-IDENTITY-END"
GENERATION_TARGET = "x_reducer_return_stats_identity"
INGREDIENTS = "return_stats_identity.ingredients.txt"

# The declared normalization rule. Dropped: these switches, and the argument of the options
# that take one (dependency-file and object paths). Rewritten: the source file and the path of
# path-valued options (location only). Everything else is bound verbatim, in order.
DROP_FLAGS = {"-c", "-MD", "-MMD", "-MP", "-MG"}
DROP_WITH_ARGUMENT = {"-o", "-MF", "-MT", "-MQ"}
PATH_OPTIONS = {"-I", "-isystem", "-iquote", "-idirafter", "-iframework", "-F", "-isysroot", "-B"}
INCLUDE_OPTIONS = {"-include", "-imacros"}
JOINED_PATH = re.compile(r"^(-I|-F|-B|--sysroot=)(.+)$")

FIXTURE_CMAKE = """\
cmake_minimum_required(VERSION 3.19)
project(PfhReturnStatsFixture C CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
include("@REPOSITORY@/cmake/ReturnStatsIdentity.cmake")
set(PFH_TEST_SOURCE_OPTIONS "-ffp-contract=off;-fno-fast-math" CACHE STRING "")
set(PFH_TEST_CONTRACT "pineforge-hpo-return-stats/v1" CACHE STRING "")
set(PFH_TEST_LIST_GENERATED OFF CACHE BOOL "")
set(PFH_TEST_DEPENDENCY OFF CACHE BOOL "")
set(PFH_TEST_LATE_SOURCE_OPTION "" CACHE STRING "")
set(PFH_TEST_LATE_TARGET_OPTION "" CACHE STRING "")
set(PFH_TEST_LATE_FLAGS "" CACHE STRING "")
add_library(x_reducer STATIC reducer.cpp)
add_executable(x_probe probe.cpp)
target_include_directories(x_probe PRIVATE "@REPOSITORY@/include")
set(extra_headers "")
if(PFH_TEST_LIST_GENERATED)
    set(extra_headers
        "${CMAKE_CURRENT_BINARY_DIR}/generated/return_stats_identity/Release/return_stats_identity_generated.hpp")
endif()
pfh_return_stats_identity(
    TARGET x_reducer
    SOURCES reducer.cpp
    HEADERS reducer.hpp ${extra_headers}
    CONTRACT "${PFH_TEST_CONTRACT}"
    SOURCE_OPTIONS ${PFH_TEST_SOURCE_OPTIONS}
    CONSUMERS x_probe)
# Everything below changes the compile command after the helper was called.
if(PFH_TEST_DEPENDENCY)
    add_library(x_dependency INTERFACE)
    target_compile_options(x_dependency INTERFACE -fno-strict-aliasing)
    target_compile_definitions(x_dependency INTERFACE PFH_X_DEPENDENCY_PROVIDED=1)
    target_link_libraries(x_reducer PRIVATE x_dependency)
endif()
if(PFH_TEST_LATE_SOURCE_OPTION)
    set_property(SOURCE reducer.cpp APPEND PROPERTY COMPILE_OPTIONS ${PFH_TEST_LATE_SOURCE_OPTION})
endif()
if(PFH_TEST_LATE_TARGET_OPTION)
    target_compile_options(x_reducer PRIVATE ${PFH_TEST_LATE_TARGET_OPTION})
endif()
if(PFH_TEST_LATE_FLAGS)
    string(APPEND CMAKE_CXX_FLAGS " ${PFH_TEST_LATE_FLAGS}")
endif()
"""
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
    '    std::cout << "bound=" << (return_stats_identity_bound() ? "true" : "false") << "\\n"\n'
    '              << "reason=" << return_stats_identity_unbound_reason() << "\\n"\n'
    '              << "identity=" << return_stats_numeric_build_identity() << "\\n"\n'
    '              << "source_digest=" << return_stats_source_digest() << "\\n"\n'
    '              << "contract=" << return_stats_contract() << "\\n"\n'
    '              << "descriptor_begin\\n" << return_stats_identity_descriptor()\n'
    '              << "descriptor_end\\n";\n'
    "    return 0;\n"
    "}\n"
)
STAND_IN_HPP = (
    "#pragma once\n"
    "#include <string_view>\n"
    "namespace pineforge::hpo {\n"
    'inline constexpr std::string_view kReturnStatsContract = "pineforge-hpo-return-stats/v1";\n'
    "double return_stats_stand_in(double value);\n"
    "}\n"
)
STAND_IN_CPP = (
    "#include <pineforge/hpo/return_stats.hpp>\n"
    "namespace pineforge::hpo { double return_stats_stand_in(double value) { return value; } }\n"
)
REAL_CORE_INTEGRATION = """

include("${CMAKE_CURRENT_SOURCE_DIR}/cmake/ReturnStatsIdentity.cmake")
target_sources(pineforge_hpo_core PRIVATE src/core/return_stats.cpp)
pfh_return_stats_identity(
    TARGET pineforge_hpo_core
    SOURCES src/core/return_stats.cpp
    HEADERS include/pineforge/hpo/return_stats.hpp
    CONTRACT "pineforge-hpo-return-stats/v1"
    SOURCE_OPTIONS -fno-fast-math -ffp-contract=off -frounding-math -fno-builtin -fno-lto
    CONSUMERS pineforge_hpo_core)
"""
REAL_CORE_LATE_CHANGES = """
add_library(pfh_x_dependency INTERFACE)
target_compile_options(pfh_x_dependency INTERFACE -fno-strict-aliasing)
target_compile_definitions(pfh_x_dependency INTERFACE PFH_X_DEPENDENCY_PROVIDED=1)
target_link_libraries(pineforge_hpo_core PRIVATE pfh_x_dependency)
set_property(SOURCE src/core/return_stats.cpp APPEND PROPERTY COMPILE_OPTIONS -fno-trapping-math)
"""


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def run(command, *, timeout=600, check=True):
    words = [str(part) for part in command]
    process = subprocess.run(words, capture_output=True, text=True, timeout=timeout)
    if check and process.returncode != 0:
        raise AssertionError(
            f"{' '.join(words)} exited with {process.returncode}\n"
            f"{process.stdout}\n{process.stderr}"
        )
    return process


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------
# The declared normalization rule, implemented independently of the CMake generator.
# ---------------------------------------------------------------------------------------------


def rewrite_path(value, base, source_root, build_root):
    path = value if os.path.isabs(value) else os.path.join(base, value)
    path = os.path.normpath(path)
    for label, root in (("build", build_root), ("src", source_root)):
        if path == root:
            return f"<{label}>"
        if path.startswith(root + "/"):
            return f"<{label}>/" + path[len(root) + 1:]
    return path


def normalize(entry, source_path, source_root, build_root):
    """Return (compiler token, kept lines, dropped tokens) of a compilation database entry."""
    directory = entry["directory"]
    tokens = shlex.split(entry["command"])
    compiler, tokens = tokens[0], tokens[1:]
    lines, dropped, pending = [], [], ""
    for token in tokens:
        if pending == "drop":
            dropped.append(token)
            pending = ""
        elif pending == "path":
            lines.append(rewrite_path(token, directory, source_root, build_root))
            pending = ""
        elif pending == "include":
            resolved = token if os.path.isabs(token) else os.path.join(directory, token)
            digest = sha256_file(resolved)
            lines.append(rewrite_path(token, directory, source_root, build_root) + "#sha256=" + digest)
            pending = ""
        elif token.startswith("@"):
            raise AssertionError("response file in a bound command")
        elif token in DROP_FLAGS:
            dropped.append(token)
        elif token in DROP_WITH_ARGUMENT:
            dropped.append(token)
            pending = "drop"
        elif token in INCLUDE_OPTIONS:
            lines.append(token)
            pending = "include"
        elif token in PATH_OPTIONS:
            lines.append(token)
            pending = "path"
        elif JOINED_PATH.match(token):
            option, value = JOINED_PATH.match(token).groups()
            lines.append(option + rewrite_path(value, directory, source_root, build_root))
        elif token == source_path:
            lines.append(rewrite_path(token, directory, source_root, build_root))
        else:
            lines.append(token)
    require(pending == "", "a command ended inside an option argument")
    return compiler, lines, dropped


def parse_ingredients(path):
    values, sources, headers = {}, [], []
    for line in Path(path).read_text().splitlines():
        if not line:
            continue
        key, _, value = line.partition("=")
        if key == "source":
            sources.append(value)
        elif key == "header":
            headers.append(value)
        else:
            values[key] = value
    return values, sources, headers


def independent_descriptor(emitted, ingredients, sources, headers, database):
    """Rebuild the descriptor of a bound build; only the compiler probe lines are borrowed."""
    banner = re.search(r"^compiler\.banner=(.*)$", emitted, re.M).group(1)
    target = re.search(r"^compiler\.target=(.*)$", emitted, re.M).group(1)
    source_root = os.path.normpath(ingredients["source.root"])
    build_root = os.path.normpath(ingredients["build.root"])
    compiler_real = os.path.realpath(ingredients["compiler.path"])
    blocks = []
    for source in sources:
        source = os.path.normpath(source)
        matches = [
            entry
            for entry in database
            if os.path.normpath(entry["file"]) == source
            and f"/{ingredients['target']}.dir/" in entry["command"]
        ]
        require(len(matches) == 1, f"expected one compile command for {source}: {matches}")
        compiler, lines, dropped = normalize(matches[0], source, source_root, build_root)
        token_real = os.path.realpath(os.path.join(matches[0]["directory"], compiler))
        require(token_real == compiler_real, "the command does not use the bound compiler")
        for token in dropped:
            require(
                token in DROP_FLAGS | DROP_WITH_ARGUMENT or not token.startswith("-"),
                f"normalization dropped a flag-like token: {token}",
            )
        display = rewrite_path(source, build_root, source_root, build_root)
        blocks.append("command " + display + "\n" + "".join(f"arg {line}\n" for line in lines))
    file_text = ""
    for kind, paths in (("source", sources), ("header", headers)):
        entries = sorted(
            f"{kind} {rewrite_path(path, build_root, source_root, build_root)} "
            f"sha256={sha256_file(path)}"
            for path in paths
        )
        file_text += "".join(entry + "\n" for entry in entries)
    return (
        f"{FORMAT}\n"
        f"contract={ingredients['contract']}\n"
        f"configuration={ingredients['configuration']}\n"
        f"compiler.id={ingredients['compiler.id']}\n"
        f"compiler.version={ingredients['compiler.version']}\n"
        f"compiler.banner={banner}\n"
        f"compiler.target={target}\n"
        f"compiler.sha256={sha256_file(compiler_real)}\n"
        f"system.name={ingredients['system.name']}\n"
        f"system.processor={ingredients['system.processor']}\n"
        + "".join(sorted(blocks))
        + file_text
        + f"source.digest={sha256_text(file_text)}\n"
    )


def check_outputs(directory, ingredients_path, database):
    """The emitted descriptor and identity must equal the independent recomputation."""
    emitted = (directory / "return_stats_identity.descriptor.txt").read_text()
    identity = (directory / "return_stats_identity.txt").read_text().strip()
    header = (directory / "return_stats_identity_generated.hpp").read_text()
    require("kBound = true;" in header, f"unexpectedly unbound:\n{emitted}")
    ingredients, sources, headers = parse_ingredients(ingredients_path)
    rebuilt = independent_descriptor(emitted, ingredients, sources, headers, database)
    require(rebuilt == emitted, f"descriptor differs from the independent rule:\n{emitted}\n---\n{rebuilt}")
    require(identity == IDENTITY_PREFIX + sha256_text(emitted), "identity is not the digest")
    return emitted, identity


# ---------------------------------------------------------------------------------------------
# Synthetic worlds: the generator as a pure function of its inputs.
# ---------------------------------------------------------------------------------------------


class World:
    def __init__(self, base, name, compiler_bytes=b"FAKE-CXX-1\n"):
        self.root = base / name
        for sub in ("src", "include", "tools", "build/out"):
            (self.root / sub).mkdir(parents=True)
        (self.root / "src/reducer.cpp").write_text(REDUCER_CPP)
        (self.root / "include/reducer.hpp").write_text(REDUCER_HPP)
        (self.root / "include/force.h").write_text("#define PFH_FORCED 1\n")
        self.compiler = self.root / "tools/cxx"
        self.compiler.write_bytes(compiler_bytes)
        self.out = self.root / "build/out"

    def arguments(self):
        root = self.root.as_posix()
        obj = "CMakeFiles/x_core.dir/src/reducer.cpp.o"
        return [
            self.compiler.as_posix(), "-DPFH_A=1", f"-I{root}/include", "-isystem",
            f"{root}/build/_deps/dep/include", "-O2", "-DNDEBUG", "-std=gnu++17",
            "-fno-fast-math", "-ffp-contract=off", "-MD", "-MT", obj, "-MF", obj + ".d",
            "-o", obj, "-c", f"{root}/src/reducer.cpp",
        ]

    def ingredients(self, **overrides):
        root = self.root.as_posix()
        values = {
            "contract": CONTRACT, "configuration": "Release", "target": "x_core",
            "compiler.path": self.compiler.as_posix(), "compiler.arg1": "",
            "compiler.id": "GNU", "compiler.version": "13.2.0", "system.name": "Linux",
            "system.processor": "x86_64", "generator": "Ninja", "multi_config": "OFF",
            "database": f"{root}/build/compile_commands.json", "database_enabled": "ON",
            "source.root": root, "build.root": f"{root}/build", "launcher.target": "",
            "rule_launch.target": "", "rule_launch.global": "", "rule_launch.directory": "",
        }
        values.update(overrides)
        return values

    def generate(self, arguments=None, entries=None, drop=(), sources=None, headers=None,
                 **overrides):
        root = self.root.as_posix()
        arguments = self.arguments() if arguments is None else arguments
        if entries is None:
            entries = [{
                "directory": f"{root}/build", "command": shlex.join(arguments),
                "file": f"{root}/src/reducer.cpp",
            }]
        database = self.root / "build/compile_commands.json"
        if entries == "none":
            database.unlink(missing_ok=True)
        else:
            database.write_text(json.dumps(entries))
        values = self.ingredients(**overrides)
        for key in drop:
            values.pop(key)
        lines = [f"{key}={value}" for key, value in values.items()]
        lines += [f"source={path}" for path in (sources or [f"{root}/src/reducer.cpp"])]
        lines += [f"header={path}" for path in (headers or [f"{root}/include/reducer.hpp"])]
        ingredients = self.root / "build/ingredients.txt"
        ingredients.write_text("\n".join(lines) + "\n")
        script = REPOSITORY / "cmake/GenerateReturnStatsIdentity.cmake"
        run([
            "cmake", f"-DPFH_RSI_INGREDIENTS={ingredients}", f"-DPFH_RSI_OUTPUT_DIR={self.out}",
            f"-DPFH_RSI_CONTRACT={CONTRACT}", "-P", script,
        ], timeout=120)
        header = (self.out / "return_stats_identity_generated.hpp").read_text()
        return {
            "bound": "kBound = true;" in header,
            "reason": re.search(r'kUnboundReason\[\] = "(.*)";', header).group(1),
            "identity": (self.out / "return_stats_identity.txt").read_text().strip(),
            "descriptor": (self.out / "return_stats_identity.descriptor.txt").read_text(),
            "header": header,
            "ingredients": ingredients,
            "database": entries,
        }


def scenario_text_hygiene(repository):
    header = (repository / "include/pineforge/hpo/return_stats_identity.hpp").read_text()
    code = re.sub(r"/\*.*?\*/", "", "\n".join(l.split("//", 1)[0] for l in header.splitlines()),
                  flags=re.S).lower()
    for forbidden in ("numeric_build_flags", "numeric_build.hpp", "tpe_", "sampler", "checkpoint",
                      "dlib", "kreturnstatscontract"):
        require(forbidden not in code, f"the public header must not reference '{forbidden}'")
    helper = "\n".join(
        "\n".join(l.split("#", 1)[0] for l in (repository / "cmake" / name).read_text().splitlines())
        for name in ("ReturnStatsIdentity.cmake", "GenerateReturnStatsIdentity.cmake")
    )
    for forbidden in (
        "target_compile_options", "target_compile_definitions", "add_compile_options",
        "add_definitions", "set_target_properties", "target_link_options",
        "set(CMAKE_CXX_FLAGS", "numeric_build_flags", "GenerateNumericBuildFlags",
        "CMAKE_CXX_FLAGS_", "TARGET_PROPERTY:${RSI_TARGET},COMPILE_OPTIONS",
    ):
        require(forbidden not in helper, f"the helper must not use or model '{forbidden}'")
    note = (repository / "docs/internal/return-stats-identity.md").read_text()
    private = re.search(r"(^|[\s`\"'(=])/(Users|home|private|tmp|var|opt|mnt|root)/", note, re.M)
    require(not private, "the note must not contain an absolute private path")
    require(repository.as_posix() not in note, "the note must not contain the repository path")
    for word in ("spot", "EC2", "AWS", "supervisor", "executor", ".executors", "Claude"):
        require(word not in note, f"the note must not mention '{word}'")
    print("PASS: header and helper stay out of the TPE identity and model no flags; note is clean")


def scenario_synthetic_bound(base):
    world = World(base, "w-base")
    first = world.generate()
    require(first["bound"], f"baseline is unbound: {first['reason']}")
    emitted, identity = check_outputs(world.out, first["ingredients"], first["database"])
    require(emitted.splitlines()[0] == FORMAT, "unexpected descriptor header")
    require("kIdentity[] = \"" + identity + "\";" in first["header"], "header lacks the identity")
    stamps = {p.name: p.stat().st_mtime_ns for p in world.out.iterdir()}
    second = world.generate()
    require(second["identity"] == first["identity"], "an unchanged rerun changed the identity")
    require(
        stamps == {p.name: p.stat().st_mtime_ns for p in world.out.iterdir()},
        "an unchanged rerun rewrote its outputs",
    )
    print("PASS: synthetic baseline binds, equals the independent rule and is stable")
    return first


def scenario_synthetic_sensitivity(base, baseline):
    def variant(name, mutate):
        world = World(base, f"w-{name}")
        arguments = mutate(world.arguments())
        result = world.generate(arguments=arguments)
        require(result["bound"], f"{name}: unbound ({result['reason']})")
        check_outputs(world.out, result["ingredients"], result["database"])
        return result["identity"]

    def replace(old, new):
        return lambda args: [new if token == old else token for token in args]

    def without(old):
        return lambda args: [token for token in args if token != old]

    def swap(first, second):
        def mutate(args):
            a, b = args.index(first), args.index(second)
            args = list(args)
            args[a], args[b] = args[b], args[a]
            return args
        return mutate

    def add(*extra):
        return lambda args: args[:-1] + list(extra) + args[-1:]

    perturbations = {
        "fast-math": add("-ffast-math"),
        "optimization": replace("-O2", "-O3"),
        "contract-flag-removed": without("-ffp-contract=off"),
        "numerical-flags-reordered": swap("-fno-fast-math", "-ffp-contract=off"),
        "definition-added": add("-DPFH_B=1"),
        "standard": replace("-std=gnu++17", "-std=c++17"),
        "architecture": add("-march=native"),
        "fma": add("-mfma"),
        "optimization-and-definition-order": swap("-O2", "-DNDEBUG"),
    }
    identities = {"baseline": baseline["identity"]}
    for name, mutate in perturbations.items():
        identities[name] = variant(name, mutate)
    require(len(set(identities.values())) == len(identities), f"collision: {identities}")

    # Source and header edits, an unlisted file, the contract, a forced include, the compiler.
    world = World(base, "w-edit")
    original = world.generate()["identity"]
    require(original == baseline["identity"], "an identical world in another place differs")
    for name in ("src/reducer.cpp", "include/reducer.hpp"):
        path = world.root / name
        text = path.read_text()
        path.write_text(text + "// edited\n")
        require(world.generate()["identity"] != original, f"editing {name} kept the identity")
        path.write_text(text)
        require(world.generate()["identity"] == original, f"restoring {name} did not restore")
    (world.root / "include/force.h").write_text("// not a reducer file\n")
    require(world.generate()["identity"] == original, "an unlisted file moved the identity")
    forced = world.arguments()
    forced[-1:-1] = ["-include", (world.root / "include/force.h").as_posix()]
    first_forced = world.generate(arguments=forced)["identity"]
    (world.root / "include/force.h").write_text("#define PFH_FORCED 2\n")
    require(
        world.generate(arguments=forced)["identity"] != first_forced,
        "a forced-include content change kept the identity",
    )
    world.compiler.write_bytes(b"FAKE-CXX-2\n")
    require(world.generate()["identity"] != original, "changed compiler bytes kept the identity")
    other = World(base, "w-contract").generate(contract="pineforge-hpo-return-stats/v2")
    require(other["reason"] == "contract_mismatch", "a contract mismatch was not refused")
    print(f"PASS: {len(identities)} flag/definition/order variants, edits and compiler bytes "
          f"move the identity")


def scenario_synthetic_normalization(base, baseline):
    # Moved trees, other object and dependency-file names, -MMD and a compiler alias are no-ops.
    moved = World(base, "w-moved-with-a-longer-name").generate()
    require(moved["identity"] == baseline["identity"], "a moved tree changed the identity")
    noop = World(base, "w-noop")
    obj = "CMakeFiles/x_core.dir/src/other-object-name.o"
    arguments = noop.arguments()
    arguments[arguments.index("-MD")] = "-MMD"
    arguments[arguments.index("-MT") + 1] = obj
    arguments[arguments.index("-MF") + 1] = obj + ".dep"
    arguments[arguments.index("-o") + 1] = obj
    require(noop.generate(arguments=arguments)["identity"] == baseline["identity"],
            "object or dependency-file names changed the identity")
    alias = World(base, "w-alias")
    alias_path = alias.root / "tools/cxx-alias"
    alias_path.write_bytes(alias.compiler.read_bytes())
    arguments = alias.arguments()
    arguments[0] = alias_path.as_posix()
    result = alias.generate(arguments=arguments, **{"compiler.path": alias_path.as_posix()})
    require(result["identity"] == baseline["identity"], "a compiler alias with equal bytes differs")
    link = World(base, "w-link")
    (link.root / "tools/cxx-link").symlink_to(link.compiler)
    arguments = link.arguments()
    arguments[0] = (link.root / "tools/cxx-link").as_posix()
    result = link.generate(arguments=arguments,
                           **{"compiler.path": (link.root / "tools/cxx-link").as_posix()})
    require(result["identity"] == baseline["identity"], "a compiler symlink differs")
    print("PASS: moved trees, object/dependency names, -MMD and compiler aliases keep the identity")


def scenario_synthetic_fail_closed(base):
    def duplicate(world):
        root = world.root.as_posix()
        entry = {"directory": f"{root}/build", "command": shlex.join(world.arguments()),
                 "file": f"{root}/src/reducer.cpp"}
        return [entry, dict(entry)]

    def other_file(world):
        root = world.root.as_posix()
        return [{"directory": f"{root}/build", "command": shlex.join(world.arguments()),
                 "file": f"{root}/src/other.cpp"}]

    def with_token(*tokens, at=-1):
        def build(world):
            arguments = world.arguments()
            arguments[at:at] = list(tokens)
            return {"arguments": arguments}
        return build

    cases = [
        ("multi_config_generator", {"multi_config": "ON"}),
        ("generator_without_compile_database", {"generator": "Xcode"}),
        ("generator_without_compile_database", {"generator": "Ninja Multi-Config"}),
        ("compile_database_disabled", {"database_enabled": "OFF"}),
        ("compiler_launcher", {"launcher.target": "ccache"}),
        ("compiler_launcher", {"rule_launch.target": "ccache"}),
        ("compiler_launcher", {"rule_launch.global": "ccache"}),
        ("compiler_launcher", {"rule_launch.directory": "ccache"}),
        ("compiler_arguments", {"compiler.arg1": "--driver-mode=g++"}),
        ("compiler_unreadable", {"compiler.path": "/nonexistent/cxx"}),
        ("compile_database_missing", {"entries": "none"}),
        ("compile_database_unreadable", {"entries": []}),
        ("compile_command_missing", {"entries": other_file}),
        ("compile_command_ambiguous", {"entries": duplicate}),
        ("compiler_mismatch", {"compiler_token": "ccache-like-wrapper"}),
        ("response_file", with_token("@flags.rsp")),
        ("unsupported_character", with_token("-DPFH_X=a;b")),
        ("unsupported_character", with_token("-DPFH_X=[1]")),
        ("forced_include_unresolved", with_token("-include", "missing-forced.h")),
        ("ingredients_incomplete", {"drop": ("target",)}),
        ("contract_mismatch", {"contract": "pineforge-hpo-return-stats/v9"}),
        ("compiler_is_script", {"script": True}),
        ("reducer_file_missing", {"missing_header": True}),
    ]
    for number, (reason, spec) in enumerate(cases):
        world = World(base, f"w-closed-{number}")
        arguments, entries, headers, drop, overrides = None, None, None, (), {}
        if callable(spec):
            arguments = spec(world)["arguments"]
        else:
            for key, value in spec.items():
                if key == "entries":
                    entries = value(world) if callable(value) else value
                elif key == "compiler_token":
                    arguments = world.arguments()
                    arguments[0] = (world.root / "tools" / value).as_posix()
                elif key == "script":
                    world.compiler.write_bytes(b"#!/bin/sh\nexec c++ \"$@\"\n")
                elif key == "missing_header":
                    headers = [(world.root / "include/missing.hpp").as_posix()]
                elif key == "drop":
                    drop = value
                else:
                    overrides[key] = value
        result = world.generate(arguments=arguments, entries=entries, headers=headers,
                                drop=drop, **overrides)
        require(not result["bound"], f"{reason}: build was bound")
        require(result["reason"] == reason, f"{reason}: got reason {result['reason']}")
        require(result["identity"] == "", f"{reason}: identity not empty")
        require(f"reason={reason}" in result["descriptor"], f"{reason}: descriptor lacks it")
        require('kIdentity[] = "";' in result["header"], f"{reason}: header carries an identity")
    print(f"PASS: {len(cases)} unbindable situations fail closed with an exact reason")


# ---------------------------------------------------------------------------------------------
# Configured projects: the real compilation database of a real build system.
# ---------------------------------------------------------------------------------------------


def available_generators():
    found = []
    if shutil.which("make"):
        found.append("Unix Makefiles")
    if shutil.which("ninja"):
        found.append("Ninja")
    require(found, "neither make nor ninja is available")
    return found


def write_fixture(directory, repository):
    directory.mkdir(parents=True)
    (directory / "CMakeLists.txt").write_text(
        FIXTURE_CMAKE.replace("@REPOSITORY@", repository.as_posix()))
    (directory / "reducer.hpp").write_text(REDUCER_HPP)
    (directory / "reducer.cpp").write_text(REDUCER_CPP)
    (directory / "probe.cpp").write_text(PROBE_CPP)


def configure(source, build, *definitions, generator=None, build_type=CONFIGURATION, check=True):
    command = ["cmake"]
    if generator:
        command += ["-G", generator]
    command += ["-S", source, "-B", build, f"-DCMAKE_BUILD_TYPE={build_type}", *definitions]
    return run(command, check=check)


def build_target(build, target, *tool_arguments):
    command = ["cmake", "--build", build, "--target", target]
    if tool_arguments:
        command += ["--", *tool_arguments]
    return run(command)


def outputs(build, build_type=CONFIGURATION):
    return build / GENERATED / build_type


def generate_and_check(source, build, *definitions, generator=None, build_type=CONFIGURATION,
                       target_name="x_reducer"):
    configure(source, build, *definitions, generator=generator, build_type=build_type)
    build_target(build, f"{target_name}_return_stats_identity")
    directory = outputs(build, build_type)
    database = json.loads((build / "compile_commands.json").read_text())
    descriptor, identity = check_outputs(directory, directory / INGREDIENTS, database)
    return descriptor, identity


def arg_lines(descriptor):
    return [line[4:] for line in descriptor.splitlines() if line.startswith("arg ")]


def scenario_fixture(repository, base, generator):
    tag = generator.replace(" ", "-")
    source = base / f"fixture-{tag}"
    write_fixture(source, repository)
    descriptor, identity = generate_and_check(source, base / f"fx-{tag}-a", generator=generator)
    lines = arg_lines(descriptor)
    for flag in ("-ffp-contract=off", "-fno-fast-math", "-DNDEBUG"):
        require(flag in lines, f"{flag} missing from the bound command: {lines}")
    require(not any(token.startswith(("-MD", "-MF", "-o")) for token in lines), "dropped token kept")
    _, again = generate_and_check(source, base / f"fx-{tag}-b", generator=generator)
    require(again == identity, "the identity depends on the build directory")
    moved = base / f"fixture-{tag}-moved"
    shutil.copytree(source, moved)
    _, moved_identity = generate_and_check(moved, base / f"fx-{tag}-moved", generator=generator)
    require(moved_identity == identity, "the identity depends on where the source tree lives")

    options = "-DPFH_TEST_SOURCE_OPTIONS="
    variants = {
        "baseline": [],
        "extra-source-option": [options + "-ffp-contract=off;-fno-fast-math;-fno-builtin"],
        "reordered-source-options": [options + "-fno-fast-math;-ffp-contract=off"],
        "dependency-usage-requirement": ["-DPFH_TEST_DEPENDENCY=ON"],
        "late-source-property": ["-DPFH_TEST_LATE_SOURCE_OPTION=-fno-trapping-math"],
        "late-target-option": ["-DPFH_TEST_LATE_TARGET_OPTION=-fno-signed-zeros"],
        "late-cxx-flags": ["-DPFH_TEST_LATE_FLAGS=-fno-associative-math"],
        "contract": ["-DPFH_TEST_CONTRACT=pineforge-hpo-return-stats/v2"],
    }
    expected_tokens = {
        "dependency-usage-requirement": ["-fno-strict-aliasing", "-DPFH_X_DEPENDENCY_PROVIDED=1"],
        "late-source-property": ["-fno-trapping-math"],
        "late-target-option": ["-fno-signed-zeros"],
        "late-cxx-flags": ["-fno-associative-math"],
    }
    identities = {"baseline": identity}
    for name, definitions in variants.items():
        if name == "baseline":
            continue
        descriptor, result = generate_and_check(
            source, base / f"fx-{tag}-{name}", *definitions, generator=generator)
        for token in expected_tokens.get(name, []):
            require(token in arg_lines(descriptor), f"{name}: {token} is not bound")
        identities[name] = result
    for build_type, flags in (("Debug", []), ("FeedAudit", ["-DCMAKE_CXX_FLAGS_FEEDAUDIT=-O1"]),
                              ("FeedAudit2", ["-DCMAKE_CXX_FLAGS_FEEDAUDIT2=-O2"])):
        _, result = generate_and_check(
            source, base / f"fx-{tag}-{build_type}", *flags, generator=generator,
            build_type=build_type)
        identities[f"configuration-{build_type}"] = result
    require(len(set(identities.values())) == len(identities), f"collision: {identities}")
    print(f"PASS [{generator}]: dependency, late-property, late-flag, configuration and contract "
          f"changes all move the identity ({len(identities)} distinct)")


def scenario_executed_command(repository, base, generator):
    tag = generator.replace(" ", "-")
    source = base / f"executed-{tag}"
    write_fixture(source, repository)
    build = base / f"ex-{tag}"
    configure(source, build, "-DPFH_TEST_DEPENDENCY=ON", generator=generator)
    process = build_target(build, "x_reducer", "-v" if generator == "Ninja" else "VERBOSE=1")
    log = process.stdout + process.stderr
    source_path = (source / "reducer.cpp").as_posix()
    commands = sorted({
        re.sub(r"^\[\d+/\d+\]\s+", "", part.strip())
        for line in log.splitlines()
        if " -c " in line and source_path in line
        for part in line.split("&&")
        if " -c " in part
    })
    require(len(commands) == 1, f"expected one executed reducer compile command: {commands}")
    database = json.loads((build / "compile_commands.json").read_text())
    entry = [e for e in database if os.path.normpath(e["file"]) == os.path.normpath(source_path)][0]
    executed = dict(entry, command=commands[0])
    roots = (os.path.normpath(source), os.path.normpath(build))
    require(
        normalize(executed, os.path.normpath(source_path), *roots)[1]
        == normalize(entry, os.path.normpath(source_path), *roots)[1],
        "the executed command differs from the compilation database entry",
    )
    print(f"PASS [{generator}]: the executed compile command equals the database entry")


def scenario_not_circular(repository, base, generator):
    source = base / "circular"
    write_fixture(source, repository)
    process = configure(source, base / "circular-build", "-DPFH_TEST_LIST_GENERATED=ON",
                        generator=generator, check=False)
    require(process.returncode != 0, "listing the generated header must fail the configure")
    require("circular" in process.stderr, f"unexpected refusal text: {process.stderr}")
    print("PASS: the generated header cannot be an input of its own identity")


def fixture_declarations(source):
    """What the fixture's own build file declares for the helper call, read back from that file."""
    text = (source / "CMakeLists.txt").read_text()
    call = re.search(r"pfh_return_stats_identity\(([^)]*)\)", text).group(1)
    keyword = lambda name: re.search(rf"\b{name}\s+(\S+)", call).group(1)
    return {
        "target": keyword("TARGET"),
        "source": keyword("SOURCES"),
        "header": keyword("HEADERS"),
        "contract": re.search(r'set\(PFH_TEST_CONTRACT\s+"([^"]*)"', text).group(1),
        "other_target": re.search(r"add_executable\((\S+)", text).group(1),
    }


def scenario_ingredients(repository, base, generator):
    """Configure only: the ingredients file carries the helper call and the finished directory.

    The writer runs at the end of the top-level directory, so it has to receive its arguments
    from the call site, and it has to read the launcher state that the project sets after the
    call. Every expected value below comes from what this test wrote into the fixture, never
    from the helper or from a previous output.
    """
    tag = generator.replace(" ", "-")
    source = base / f"ingredients-{tag}"
    write_fixture(source, repository)
    declared = fixture_declarations(source)
    target, other = declared["target"], declared["other_target"]
    require(target != other, "the fixture needs a second target to tell the bindings apart")

    # Launcher state set after the helper call. The other target gets values of its own, so a
    # launcher in the file can only have been read from the declared target.
    expected = {
        "launcher.target": "pfh-test-target-launcher",
        "rule_launch.target": "pfh-test-target-rule-launch",
        "rule_launch.global": "pfh-test-global-rule-launch",
        "rule_launch.directory": "pfh-test-directory-rule-launch",
    }
    other_values = ("pfh-test-other-launcher", "pfh-test-other-rule-launch")
    properties = [
        (f"TARGET {target}", "CXX_COMPILER_LAUNCHER", expected["launcher.target"]),
        (f"TARGET {target}", "RULE_LAUNCH_COMPILE", expected["rule_launch.target"]),
        ("GLOBAL", "RULE_LAUNCH_COMPILE", expected["rule_launch.global"]),
        ("DIRECTORY", "RULE_LAUNCH_COMPILE", expected["rule_launch.directory"]),
        (f"TARGET {other}", "CXX_COMPILER_LAUNCHER", other_values[0]),
        (f"TARGET {other}", "RULE_LAUNCH_COMPILE", other_values[1]),
    ]
    with open(source / "CMakeLists.txt", "a") as handle:
        for scope, name, value in properties:
            handle.write(f"set_property({scope} PROPERTY {name} {value})\n")

    build = base / f"ingredients-build-{tag}"
    process = configure(source, build, generator=generator, check=False)
    require(process.returncode == 0,
            f"[{generator}] the configure failed:\n{process.stdout}\n{process.stderr}")
    path = outputs(build) / INGREDIENTS
    require(path.is_file(), f"[{generator}] the ingredients file was not written: {path}")
    text = path.read_text()
    require(text.strip(), f"[{generator}] the ingredients file is empty")
    require("$<" not in text, f"[{generator}] a generator expression was left unevaluated:\n{text}")
    values, sources, headers = parse_ingredients(path)
    missing = sorted(set(World(base, f"keys-{tag}").ingredients()) - set(values))
    require(not missing, f"[{generator}] ingredient lines missing: {missing}\n{text}")

    wanted = {
        "target": target,
        "contract": declared["contract"],
        "configuration": CONFIGURATION,
        "generator": generator,
        "database_enabled": "ON",
        **expected,
    }
    for key, value in wanted.items():
        require(values[key] == value,
                f"[{generator}] {key}: wanted {value!r}, the file has {values[key]!r}")
    real = os.path.realpath
    require(real(values["source.root"]) == real(source), f"[{generator}] source.root differs")
    require(real(values["build.root"]) == real(build), f"[{generator}] build.root differs")
    require([real(item) for item in sources] == [real(source / declared["source"])],
            f"[{generator}] the source lines differ from the declared SOURCES: {sources}")
    require([real(item) for item in headers] == [real(source / declared["header"])],
            f"[{generator}] the header lines differ from the declared HEADERS: {headers}")
    require(not any(value in text for value in other_values),
            f"[{generator}] a launcher of another target was bound:\n{text}")
    print(f"PASS [{generator}]: the ingredients file holds the declared target, contract, files "
          f"and the launcher state set after the call")


def scenario_database_switch(repository, base, generator):
    """Configure only: the compilation-database switch in three cases, each checked two ways.

    The ingredients file reports what the helper saw, and compile_commands.json reports what
    CMake itself wrote at generate time. Neither one is read as the expectation of the other:
    both are compared against the expected value of the case.
    """
    # The documented default of the helper, from the comment above its switch in
    # cmake/ReturnStatsIdentity.cmake: "A project that left the switch undefined gets it on".
    default_enabled = "ON"
    off = "-DCMAKE_EXPORT_COMPILE_COMMANDS=OFF"
    on = "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"
    cases = (
        ("default", (), default_enabled),
        ("explicit-off", (off,), off.partition("=")[2]),
        ("explicit-on", (on,), on.partition("=")[2]),
    )
    tag = generator.replace(" ", "-")
    for case, definitions, expected in cases:
        source = base / f"database-{case}-{tag}"
        build = base / f"database-{case}-build-{tag}"
        write_fixture(source, repository)
        process = configure(source, build, *definitions, generator=generator, check=False)
        require(process.returncode == 0,
                f"[{generator}] {case}: the configure failed:\n"
                f"{process.stdout}\n{process.stderr}")
        path = outputs(build) / INGREDIENTS
        require(path.is_file(),
                f"[{generator}] {case}: the ingredients file was not written: {path}")
        text = path.read_text()
        require(text.strip(), f"[{generator}] {case}: the ingredients file is empty")
        values, sources, headers = parse_ingredients(path)
        require("database_enabled" in values,
                f"[{generator}] {case}: no database_enabled line:\n{text}")
        require(values["database_enabled"] == expected,
                f"[{generator}] {case}: wanted database_enabled {expected!r}, "
                f"the file has {values['database_enabled']!r}")
        written = (build / "compile_commands.json").is_file()
        require(written == (values["database_enabled"] == "ON"),
                f"[{generator}] {case}: compile_commands.json present={written} disagrees with "
                f"database_enabled={values['database_enabled']!r}")
        require(written == (expected == "ON"),
                f"[{generator}] {case}: compile_commands.json present={written} disagrees with "
                f"the expected {expected!r}")
        print(f"PASS [{generator}] {case}: database_enabled="
              f"{values['database_enabled']} and compile_commands.json present={written}")


def ingredients_group(repository, base, generators):
    for generator in generators:
        scenario_ingredients(repository, base, generator)
        scenario_database_switch(repository, base, generator)


def scenario_api(repository, base, generator):
    source = base / "api"
    write_fixture(source, repository)
    build = base / "api-build"
    configure(source, build, generator=generator)
    build_target(build, "x_probe")
    output = run([build / "x_probe"]).stdout
    field = lambda name: re.search(rf"^{name}=(.*)$", output, re.M).group(1)
    descriptor = output.split("descriptor_begin\n", 1)[1].rsplit("descriptor_end\n", 1)[0]
    require(field("bound") == "true" and field("reason") == "", "the probe reports unbound")
    require(field("contract") == CONTRACT, "unexpected contract")
    directory = outputs(build)
    require(descriptor == (directory / "return_stats_identity.descriptor.txt").read_text(),
            "the compiled descriptor differs from the retained file")
    require(field("identity") == (directory / "return_stats_identity.txt").read_text().strip(),
            "the compiled identity differs from the file")
    require(field("identity") == IDENTITY_PREFIX + sha256_text(descriptor), "identity is not the digest")
    print("PASS: the compiled accessors return the retained descriptor and its digest")


def scenario_real_core(repository, dlib, base, generator):
    definitions = (
        "-DPINEFORGE_HPO_BUILD_TESTS=OFF",
        "-DPINEFORGE_HPO_BUILD_NATIVE_CLI=OFF",
        "-DPINEFORGE_HPO_BUILD_ENGINE_ADAPTER=OFF",
        f"-DFETCHCONTENT_SOURCE_DIR_DLIB={dlib}",
    )

    # The integrated root already calls the helper once (between the marker lines), so appending
    # REAL_CORE_INTEGRATION to a copy of it would create the generation target twice and fail the
    # configure. Against such a root the baseline cuts the marked block out, "x-only" uses the root
    # as it is, and only the late changes of "x-late" are appended after the root's own call.
    already_integrated = "pfh_return_stats_identity(" in (repository / "CMakeLists.txt").read_text()

    def copy_tree(destination, integration):
        destination.mkdir(parents=True)
        for name in ("CMakeLists.txt", "VERSION", "cmake", "include", "src", "third_party"):
            origin = repository / name
            if origin.is_dir():
                shutil.copytree(origin, destination / name)
            else:
                shutil.copy2(origin, destination / name)
        path = destination / "CMakeLists.txt"
        if integration is None and already_integrated:
            text = path.read_text()
            begin, end = text.find(MARK_BEGIN), text.find(MARK_END)
            require(begin != -1 and end > begin,
                    "the return-statistics block of the root is not delimited by its marker lines")
            path.write_text(text[:begin] + text[end + len(MARK_END):])
        if integration is not None:
            # The reducer files of the repository are used when present, else stand-ins.
            if not (destination / "src/core/return_stats.cpp").exists():
                (destination / "src/core/return_stats.cpp").write_text(STAND_IN_CPP)
            if not (destination / "include/pineforge/hpo/return_stats.hpp").exists():
                (destination / "include/pineforge/hpo/return_stats.hpp").write_text(STAND_IN_HPP)
            appended = integration[len(REAL_CORE_INTEGRATION):] if already_integrated \
                else integration
            if appended:
                with open(path, "a") as handle:
                    handle.write(appended)

    trees = {
        "baseline": (None, CONFIGURATION),
        "x-only": (REAL_CORE_INTEGRATION, CONFIGURATION),
        "x-late": (REAL_CORE_INTEGRATION + REAL_CORE_LATE_CHANGES, "FeedAudit"),
    }
    builds = {}
    for label, (integration, build_type) in trees.items():
        source = base / f"core-{label}"
        copy_tree(source, integration)
        build = base / f"core-build-{label}"
        extra = ("-DCMAKE_CXX_FLAGS_FEEDAUDIT=-O1",) if build_type == "FeedAudit" else ()
        configure(source, build, *definitions, *extra, generator=generator, build_type=build_type)
        build_target(build, "pineforge_hpo_numeric_flags")
        builds[label] = (build, build_type)

    def tpe_files(label):
        build, build_type = builds[label]
        directory = build / "generated" / build_type / "CXX"
        return tuple((directory / name).read_bytes()
                     for name in ("numeric_build_flags.txt", "numeric_build_flags.hpp"))

    require(tpe_files("x-only") == tpe_files("baseline"), "adding X changed the TPE flag identity")
    require(not (builds["baseline"][0] / GENERATED).exists(), "the baseline carries X outputs")

    identities = {}
    for label in ("x-only", "x-late"):
        build, build_type = builds[label]
        build_target(build, "pineforge_hpo_core_return_stats_identity")
        directory = outputs(build, build_type)
        database = json.loads((build / "compile_commands.json").read_text())
        descriptor, identity = check_outputs(directory, directory / INGREDIENTS, database)
        lines = arg_lines(descriptor)
        for flag in ("-fno-fast-math", "-ffp-contract=off", "-frounding-math", "-fno-builtin"):
            require(flag in lines, f"{label}: {flag} is not bound")
        if label == "x-late":
            for token in ("-fno-strict-aliasing", "-DPFH_X_DEPENDENCY_PROVIDED=1",
                          "-fno-trapping-math", "-O1"):
                require(token in lines, f"{label}: {token} is not bound")
        identities[label] = identity
    require(identities["x-only"] != identities["x-late"], "late changes did not move the identity")

    # An X source edit moves X and leaves the TPE bytes alone.
    reducer = base / "core-x-only/src/core/return_stats.cpp"
    reducer.write_text(reducer.read_text() + "// edited\n")
    build, build_type = builds["x-only"]
    build_target(build, "pineforge_hpo_numeric_flags")
    build_target(build, "pineforge_hpo_core_return_stats_identity")
    require(outputs(build, build_type).joinpath("return_stats_identity.txt").read_text().strip()
            != identities["x-only"], "editing the reducer did not move X")
    require(tpe_files("x-only") == tpe_files("baseline"), "an X source edit changed the TPE identity")
    print("PASS: real core target: actual commands carry dependency, late-source and final-"
          "configuration flags; TPE flag identity is byte-identical with X added")


GENERATOR_TOKEN = {"Unix Makefiles": "makefiles", "Ninja": "ninja"}


def synthetic_group(base):
    baseline = scenario_synthetic_bound(base)
    scenario_synthetic_sensitivity(base, baseline)
    scenario_synthetic_normalization(base, baseline)
    scenario_synthetic_fail_closed(base)


def scenario_plan(dlib, generators):
    """Every scenario of the default run as (token, step(base)), in the default order.

    The default run executes all of them. `--only` selects a subset by token so that the long
    real-project scenarios can be split into separately receipted proofs, and `--list` prints the
    tokens, so a proof can show that its parts are exactly the default run's scenarios. Each
    scenario writes only into its own subdirectories of `base`: a subset runs the same code on
    the same inputs as the corresponding part of the default run.
    """
    steps = [("text_hygiene", lambda base: scenario_text_hygiene(REPOSITORY)),
             ("synthetic", synthetic_group),
             ("ingredients", lambda base: ingredients_group(REPOSITORY, base, generators))]
    for generator in generators:
        label = GENERATOR_TOKEN[generator]
        steps.append((f"fixture:{label}",
                      lambda base, g=generator: scenario_fixture(REPOSITORY, base, g)))
        steps.append((f"executed:{label}",
                      lambda base, g=generator: scenario_executed_command(REPOSITORY, base, g)))
    steps.append(("not_circular",
                  lambda base: scenario_not_circular(REPOSITORY, base, generators[0])))
    steps.append(("api", lambda base: scenario_api(REPOSITORY, base, generators[0])))
    for generator in generators:
        steps.append((f"real_core:{GENERATOR_TOKEN[generator]}",
                      lambda base, g=generator: scenario_real_core(
                          REPOSITORY, dlib, base / g.replace(" ", "-"), g)))
    return steps


def main():
    global REPOSITORY
    arguments = sys.argv[1:]
    listing = "--list" in arguments
    arguments = [argument for argument in arguments if argument != "--list"]
    only = None
    if "--only" in arguments:
        at = arguments.index("--only")
        if at + 1 >= len(arguments):
            print(__doc__)
            return 2
        only = [token for token in arguments[at + 1].split(",") if token]
        del arguments[at:at + 2]
    if len(arguments) != 2:
        print(__doc__)
        return 2
    REPOSITORY, dlib = (Path(argument).resolve() for argument in arguments)
    plan = scenario_plan(dlib, available_generators())
    tokens = [token for token, _ in plan]
    if listing:
        print("\n".join(tokens))
        return 0
    if only is not None:
        require(only and len(set(only)) == len(only) and set(only) <= set(tokens),
                f"--only needs distinct known tokens; known: {tokens}")
    with tempfile.TemporaryDirectory(prefix="pfh-rsi-") as temporary:
        base = Path(temporary)
        for token, step in plan:
            if only is None or token in only:
                print(f"SCENARIO {token}", flush=True)
                step(base)
                print(f"SCENARIO-DONE {token}", flush=True)
    if only is None:
        print("PASS: return-statistics build identity")
    else:
        print("PASS-PARTIAL: return-statistics build identity, selected scenarios: "
              + ",".join(only))
    return 0


REPOSITORY = Path(".")

if __name__ == "__main__":
    raise SystemExit(main())
