"""Sobol numeric build identity: bound to the generated compile commands, fail-closed otherwise.

Usage: test_sobol_identity.py REPOSITORY_ROOT DLIB_SOURCE_DIR [--list] [--only TOKEN[,TOKEN...]]

Without options every scenario runs, in the same order as before. `--list` prints the scenario
tokens of this host (one per line, nothing is run); `--only` runs just the named tokens, in plan
order, and ends with PASS-PARTIAL instead of PASS.

The identity is derived from the compile command that the build system generated for each Sobol
C++ translation unit and each C translation unit of the portable-math target. This test keeps an
independent implementation of the declared normalization rule (imported from the return-statistics
identity test, which owns the rule's test copy) and compares it with the generator's output, on
synthetic compilation databases (the generator is a pure function of its inputs), on configured
fixture projects, and on copies of the real project. It checks the build binding only. It does not
prove arithmetic, repeat or worker-count invariance, and it does not replace the reference-value
proofs or the measured-pair qualification.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "pfh_x_identity_test", HERE / "test_return_stats_identity.py")
X = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(X)

require, run = X.require, X.run
sha256_file, sha256_text = X.sha256_file, X.sha256_text
rewrite_path = X.rewrite_path

JOINED_INCLUDE = re.compile(r"^(-include|-imacros)([^-].*)$")
JOINED_OPTIONS = re.compile(
    r"^(-I|-F|-B|--sysroot=|-isystem|-iquote|-idirafter|-iframework|-isysroot)(.+)$")

CONTRACT = "portable-sobol-v1"
FORMAT = "pineforge-hpo-sobol-identity/v1"
CONFIGURATION = "Release"
GENERATED = Path("generated/sobol_identity")
GENERATION_TARGET = "x_core_sobol_identity"
INGREDIENTS = "sobol_identity.ingredients.txt"
MARK_BEGIN = "# PFH-SOBOL-IDENTITY-BEGIN"
MARK_END = "# PFH-SOBOL-IDENTITY-END"
STRICT = ("-fno-fast-math", "-ffp-contract=off")

REPOSITORY = Path(".")


def helper_inventory(repository):
    """The helper's own inventory, so the test cannot drift from it."""
    text = (repository / "cmake/SobolIdentity.cmake").read_text()
    lists = {}
    for name in ("CXX_SOURCES", "HEADERS"):
        body = re.search(rf"set\(_PFH_SOBOL_{name}\s+(.*?)\)", text, re.S).group(1)
        lists[name] = body.split()
    return lists["CXX_SOURCES"], lists["HEADERS"]


def provider_inventory(repository):
    """The shared-helper providers the helper binds besides the four Sobol units."""
    text = (repository / "cmake/SobolIdentity.cmake").read_text()
    return re.search(r"set\(_PFH_SOBOL_PROVIDER_SOURCES\s+(.*?)\)", text, re.S).group(1).split()


SHARED_HELPER_HEADERS = ("src/core/numeric_build.hpp", "src/core/portable_math.hpp",
                         "src/core/portable_grid.hpp")
STRICT_RECIPE = '"-fno-fast-math;-ffp-contract=off;-frounding-math;-fno-builtin;-fno-lto"'


FIXTURE_CMAKE = """\
cmake_minimum_required(VERSION 3.19)
project(PfhSobolFixture C CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
include("@REPOSITORY@/cmake/SobolIdentity.cmake")
set(PFH_TEST_DEPENDENCY OFF CACHE BOOL "")
set(PFH_TEST_LATE_SOURCE_OPTION "" CACHE STRING "")
set(PFH_TEST_LATE_MATH_OPTION "" CACHE STRING "")
set(PFH_TEST_LATE_CXX_FLAGS "" CACHE STRING "")
set(PFH_TEST_LATE_C_FLAGS "" CACHE STRING "")
set(PFH_TEST_LATE_PROVIDER_OPTION "" CACHE STRING "")
add_library(x_math STATIC math/m1.c math/m2.c)
target_compile_options(x_math PRIVATE -fno-fast-math -ffp-contract=off -frounding-math
    "-include${CMAKE_CURRENT_SOURCE_DIR}/third_party/core_math/portable.h")
set_target_properties(x_math PROPERTIES C_STANDARD 11 C_STANDARD_REQUIRED ON)
add_library(x_core STATIC src/core/sobol_engine.cpp src/core/sobol_identity.cpp
    src/core/sobol_mapper.cpp src/core/sobol_sampler.cpp
    src/core/search_space.cpp src/core/tpe_sampler.cpp)
# The two providers carry the same per-source recipe as in the real project.
set_source_files_properties(src/core/search_space.cpp src/core/tpe_sampler.cpp PROPERTIES
    COMPILE_OPTIONS "-fno-fast-math;-ffp-contract=off;-frounding-math;-fno-builtin;-fno-lto")
add_executable(x_probe probe.cpp)
target_link_libraries(x_probe PRIVATE x_core x_math)
pfh_sobol_identity(TARGET x_core PORTABLE_MATH_TARGET x_math CONSUMERS x_probe)
# Everything below changes the generated commands after the helper was called.
if(PFH_TEST_DEPENDENCY)
    add_library(x_dependency INTERFACE)
    target_compile_options(x_dependency INTERFACE -fno-strict-aliasing)
    target_compile_definitions(x_dependency INTERFACE PFH_X_DEPENDENCY_PROVIDED=1)
    target_link_libraries(x_core PRIVATE x_dependency)
endif()
if(PFH_TEST_LATE_SOURCE_OPTION)
    set_property(SOURCE src/core/sobol_mapper.cpp APPEND PROPERTY COMPILE_OPTIONS
        ${PFH_TEST_LATE_SOURCE_OPTION})
endif()
if(PFH_TEST_LATE_MATH_OPTION)
    target_compile_options(x_math PRIVATE ${PFH_TEST_LATE_MATH_OPTION})
endif()
if(PFH_TEST_LATE_CXX_FLAGS)
    string(APPEND CMAKE_CXX_FLAGS " ${PFH_TEST_LATE_CXX_FLAGS}")
endif()
if(PFH_TEST_LATE_C_FLAGS)
    string(APPEND CMAKE_C_FLAGS " ${PFH_TEST_LATE_C_FLAGS}")
endif()
if(PFH_TEST_LATE_PROVIDER_OPTION)
    set_property(SOURCE src/core/tpe_sampler.cpp APPEND PROPERTY COMPILE_OPTIONS
        ${PFH_TEST_LATE_PROVIDER_OPTION})
endif()
"""
FIXTURE_IDENTITY_CPP = (
    "#include <sobol_identity_generated.hpp>\n"
    'extern "C" const char* pfh_fixture_digest() {\n'
    "    return pineforge::hpo::detail::sobol_identity_generated::kBuildDigest;\n"
    "}\n"
)
FIXTURE_PROBE_CPP = (
    "#include <iostream>\n"
    'extern "C" const char* pfh_fixture_digest();\n'
    "int main() { std::cout << pfh_fixture_digest() << \"\\n\"; return 0; }\n"
)
FIXTURE_MATH_C = "double pfh_fixture_{n}(double value) {{ return value * 2.0 + 1.0; }}\n"

# Appended to a copy of the real root project that has no Sobol integration yet. The marker
# lines let this test cut the block out again to build the baseline of the invariance check.
SOBOL_INTEGRATION = f"""

{MARK_BEGIN}
include("${{CMAKE_CURRENT_SOURCE_DIR}}/cmake/SobolIdentity.cmake")
target_sources(pineforge_hpo_core PRIVATE
    src/core/sobol_engine.cpp
    src/core/sobol_identity.cpp
    src/core/sobol_mapper.cpp
    src/core/sobol_sampler.cpp)
pfh_sobol_identity(
    TARGET pineforge_hpo_core
    PORTABLE_MATH_TARGET pineforge_hpo_portable_math
    SOURCE_OPTIONS -fno-fast-math -ffp-contract=off -frounding-math -fno-builtin -fno-lto)
{MARK_END}
"""


# ---------------------------------------------------------------------------------------------
# Independent recomputation of the descriptor from the ingredients and the database.
# ---------------------------------------------------------------------------------------------


def normalize(entry, source_path, source_root, build_root):
    """The declared rule of cmake/CompileCommandBinding.cmake, implemented independently.

    It is the rule of the return-statistics identity test plus the joined forms of the forced
    includes and path options. Returns (compiler token, kept lines, dropped tokens)."""
    directory = entry["directory"]
    tokens = shlex.split(entry["command"])
    compiler, tokens = tokens[0], tokens[1:]
    lines, dropped, pending = [], [], ""

    def forced(path):
        resolved = path if os.path.isabs(path) else os.path.join(directory, path)
        return rewrite_path(path, directory, source_root, build_root) + "#sha256=" + \
            sha256_file(resolved)

    for token in tokens:
        if pending == "drop":
            dropped.append(token)
            pending = ""
        elif pending == "path":
            lines.append(rewrite_path(token, directory, source_root, build_root))
            pending = ""
        elif pending == "include":
            lines.append(forced(token))
            pending = ""
        elif token.startswith("@"):
            raise AssertionError("response file in a bound command")
        elif token in X.DROP_FLAGS:
            dropped.append(token)
        elif token in X.DROP_WITH_ARGUMENT:
            dropped.append(token)
            pending = "drop"
        elif token in X.INCLUDE_OPTIONS:
            lines.append(token)
            pending = "include"
        elif token in X.PATH_OPTIONS:
            lines.append(token)
            pending = "path"
        elif JOINED_INCLUDE.match(token):
            option, value = JOINED_INCLUDE.match(token).groups()
            lines.append(option + forced(value))
        elif JOINED_OPTIONS.match(token):
            option, value = JOINED_OPTIONS.match(token).groups()
            lines.append(option + rewrite_path(value, directory, source_root, build_root))
        elif token == source_path:
            lines.append(rewrite_path(token, directory, source_root, build_root))
        else:
            lines.append(token)
    require(pending == "", "a command ended inside an option argument")
    return compiler, lines, dropped


def parse_ingredients(path):
    values, cxx, c, headers, providers = {}, [], [], [], []
    for line in Path(path).read_text().splitlines():
        if not line:
            continue
        key, _, value = line.partition("=")
        if key == "source.cxx":
            cxx.append(value)
        elif key == "source.provider":
            providers.append(value)
        elif key == "source.c":
            c.append(value)
        elif key == "header":
            headers.append(value)
        else:
            values[key] = value
    return values, cxx, c, headers, providers


def independent_descriptor(emitted, ing, cxx, c, headers, providers, database):
    """Rebuild the descriptor of a bound build; only the compiler probe lines are borrowed."""
    def borrowed(name):
        return re.search(rf"^{re.escape(name)}=(.*)$", emitted, re.M).group(1)

    source_root = os.path.normpath(ing["source.root"])
    build_root = os.path.normpath(ing["build.root"])
    real = {"cxx": os.path.realpath(ing["cxx.path"]), "c": os.path.realpath(ing["c.path"])}
    blocks = []
    # Providers are compiled by the core target with the C++ driver; their label differs.
    groups = (("cxx", "cxx", cxx, ing["core.target"]),
              ("provider", "cxx", providers, ing["core.target"]),
              ("c", "c", c, ing["math.target"]))
    for kind, driver, sources, target in groups:
        for source in sources:
            source = os.path.normpath(source)
            matches = [e for e in database if os.path.normpath(e["file"]) == source
                       and f"/{target}.dir/" in e["command"]]
            require(len(matches) == 1, f"expected one compile command for {source}: {matches}")
            compiler, lines, dropped = normalize(matches[0], source, source_root, build_root)
            token_real = os.path.realpath(os.path.join(matches[0]["directory"], compiler))
            require(token_real == real[driver], f"{source}: the command uses another compiler")
            for token in dropped:
                require(token in X.DROP_FLAGS | X.DROP_WITH_ARGUMENT or not token.startswith("-"),
                        f"normalization dropped a flag-like token: {token}")
            display = rewrite_path(source, build_root, source_root, build_root)
            blocks.append(f"command {kind} {display}\n" + "".join(f"arg {l}\n" for l in lines))
    file_text = ""
    for label, paths in (("source", cxx + providers + c), ("header", headers)):
        entries = sorted(
            f"{label} {rewrite_path(p, build_root, source_root, build_root)} "
            f"sha256={sha256_file(p)}" for p in set(paths))
        file_text += "".join(entry + "\n" for entry in entries)
    return (
        f"{FORMAT}\ncontract={ing['contract']}\nconfiguration={ing['configuration']}\n"
        f"system.name={ing['system.name']}\nsystem.processor={ing['system.processor']}\n"
        f"compiler.cxx.id={ing['cxx.id']}\ncompiler.cxx.version={ing['cxx.version']}\n"
        f"compiler.cxx.banner={borrowed('compiler.cxx.banner')}\n"
        f"compiler.cxx.target={borrowed('compiler.cxx.target')}\n"
        f"compiler.cxx.sha256={sha256_file(real['cxx'])}\n"
        f"compiler.c.id={ing['c.id']}\ncompiler.c.version={ing['c.version']}\n"
        f"compiler.c.banner={borrowed('compiler.c.banner')}\n"
        f"compiler.c.target={borrowed('compiler.c.target')}\n"
        f"compiler.c.sha256={sha256_file(real['c'])}\n"
        + "".join(sorted(blocks)) + file_text + f"source.digest={sha256_text(file_text)}\n")


def check_outputs(directory, ingredients_path, database):
    """The emitted descriptor and digest must equal the independent recomputation."""
    emitted = (directory / "sobol_identity.descriptor.txt").read_text()
    digest = (directory / "sobol_identity.txt").read_text().strip()
    header = (directory / "sobol_identity_generated.hpp").read_text()
    require("kBound = true;" in header, f"unexpectedly unbound:\n{emitted}")
    ing, cxx, c, headers, providers = parse_ingredients(ingredients_path)
    require(providers, "the ingredients carry no shared-helper provider")
    rebuilt = independent_descriptor(emitted, ing, cxx, c, headers, providers, database)
    require(rebuilt == emitted, f"descriptor differs from the independent rule:\n{emitted}\n---\n{rebuilt}")
    require(digest == sha256_text(emitted), "the digest is not the SHA-256 of the descriptor")
    require(f'kBuildDigest[] = "{digest}";' in header, "the header lacks the digest")
    return emitted, digest


def blocks_of(descriptor):
    parts = re.split(r"(?m)^(?=command )", descriptor.split("\nsource ", 1)[0])
    return [p for p in parts if p.startswith("command ")]


def require_strict(descriptor, minimum_c=1):
    blocks = blocks_of(descriptor)
    cxx = [b for b in blocks if b.startswith("command cxx ")]
    c = [b for b in blocks if b.startswith("command c ")]
    providers = [b for b in blocks if b.startswith("command provider ")]
    require(len(cxx) == 4, f"expected four bound C++ units, got {len(cxx)}")
    require(len(c) >= minimum_c, f"too few bound C units: {len(c)}")
    expected = provider_inventory(REPOSITORY)
    require(len(providers) == len(expected),
            f"expected {len(expected)} bound providers, got {len(providers)}")
    for relative in expected:
        require(any(b.splitlines()[0].endswith(f"<src>/{relative}") for b in providers),
                f"the provider {relative} has no bound command")
        require(f"\nsource <src>/{relative} sha256=" in descriptor,
                f"the provider {relative} has no bound source digest")
    # Every bound command, the providers' included, carries the strict arithmetic recipe: a
    # provider without it would still be bound, but it would be a finding worth a failure here.
    for block in blocks:
        for flag in STRICT:
            require(f"arg {flag}\n" in block, f"{flag} missing from {block.splitlines()[0]}")
        require("arg -ffast-math\n" not in block, "fast-math in a bound command")


# ---------------------------------------------------------------------------------------------
# Synthetic worlds: the generator as a pure function of its inputs.
# ---------------------------------------------------------------------------------------------


class World:
    C_SOURCES = ("src/core/portable_math_canary.c", "third_party/core_math/log/log.c",
                 "third_party/core_math/exp/exp.c")

    def __init__(self, base, name):
        self.root = base / name
        self.cxx_rel, self.headers_rel = helper_inventory(REPOSITORY)
        self.provider_rel = provider_inventory(REPOSITORY)
        for rel in self.cxx_rel + self.provider_rel + list(self.C_SOURCES) + self.headers_rel:
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"// {rel}\n")
        (self.root / "build").mkdir()
        (self.root / "tools").mkdir()
        self.cxx = self.root / "tools/cxx"
        self.cc = self.root / "tools/cc"
        self.cxx.write_bytes(b"FAKE-CXX-1\n")
        self.cc.write_bytes(b"FAKE-CC-1\n")
        self.out = self.root / "build/out"

    def groups(self, c_rel=None):
        """The three translation-unit groups: Sobol units, shared-helper providers, C units."""
        return (("cxx", self.cxx_rel), ("provider", self.provider_rel),
                ("c", list(self.C_SOURCES) if c_rel is None else c_rel))

    def args(self, kind, rel):
        root = self.root.as_posix()
        cplus = kind in ("cxx", "provider")  # providers are core units built by the C++ driver
        obj = f"CMakeFiles/{'x_core' if cplus else 'x_math'}.dir/{rel}.o"
        common = ["-DPFH_A=1", f"-I{root}/include", "-isystem", f"{root}/build/_deps/dep/include",
                  "-O2", "-DNDEBUG", "-fno-fast-math", "-ffp-contract=off", "-frounding-math",
                  "-fno-builtin", "-fno-lto", "-MD", "-MT", obj, "-MF", obj + ".d", "-o", obj,
                  "-c", f"{root}/{rel}"]
        if cplus:
            return [self.cxx.as_posix(), "-std=gnu++17"] + common
        # The real portable-math target passes the forced include in the joined form.
        return [self.cc.as_posix(), "-std=gnu11",
                f"-include{root}/third_party/core_math/portable.h"] + common

    def ingredients(self, **overrides):
        root = self.root.as_posix()
        values = {
            "contract": CONTRACT, "configuration": "Release", "core.target": "x_core",
            "math.target": "x_math", "cxx.path": self.cxx.as_posix(), "cxx.arg1": "",
            "cxx.id": "GNU", "cxx.version": "13.2.0", "c.path": self.cc.as_posix(), "c.arg1": "",
            "c.id": "GNU", "c.version": "13.2.0", "system.name": "Linux",
            "system.processor": "x86_64", "generator": "Ninja", "multi_config": "OFF",
            "database": f"{root}/build/compile_commands.json", "database_enabled": "ON",
            "source.root": root, "build.root": f"{root}/build", "launcher.core": "",
            "launcher.math": "", "rule_launch.global": "", "rule_launch.directory": "",
            "rule_launch.core": "", "rule_launch.math": "",
        }
        values.update(overrides)
        return values

    def generate(self, mutate=None, entries=None, drop=(), c_sources=None, headers=None,
                 providers=None, **overrides):
        root = self.root.as_posix()
        c_rel = list(self.C_SOURCES) if c_sources is None else c_sources
        provider_rel = self.provider_rel if providers is None else providers
        if entries is None:
            entries = []
            for kind, rels in self.groups(c_rel):
                for rel in rels:
                    args = self.args(kind, rel)
                    if mutate:
                        args = mutate(kind, rel, args)
                    entries.append({"directory": f"{root}/build", "command": shlex.join(args),
                                    "file": f"{root}/{rel}"})
        database = self.root / "build/compile_commands.json"
        if entries == "none":
            database.unlink(missing_ok=True)
        else:
            database.write_text(json.dumps(entries))
        values = self.ingredients(**overrides)
        for key in drop:
            values.pop(key)
        lines = [f"{k}={v}" for k, v in values.items()]
        lines += [f"source.cxx={root}/{rel}" for rel in self.cxx_rel]
        lines += [f"source.provider={root}/{rel}" for rel in provider_rel]
        lines += [f"source.c={root}/{rel}" for rel in c_rel]
        lines += [f"header={root}/{rel}" for rel in (headers or self.headers_rel)]
        ingredients = self.root / "build/ingredients.txt"
        ingredients.write_text("\n".join(lines) + "\n")
        run(["cmake", f"-DPFH_SOBOL_INGREDIENTS={ingredients}", f"-DPFH_SOBOL_OUTPUT_DIR={self.out}",
             f"-DPFH_SOBOL_CONTRACT={CONTRACT}", "-P", REPOSITORY / "cmake/GenerateSobolIdentity.cmake"],
            timeout=120)
        header = (self.out / "sobol_identity_generated.hpp").read_text()
        return {
            "bound": "kBound = true;" in header,
            "reason": re.search(r'kUnboundReason\[\] = "(.*)";', header).group(1),
            "digest": (self.out / "sobol_identity.txt").read_text().strip(),
            "descriptor": (self.out / "sobol_identity.descriptor.txt").read_text(),
            "header": header, "ingredients": ingredients, "database": entries,
        }


def only(rel_suffix, change):
    """Mutation of the commands of the sources ending in rel_suffix."""
    return lambda kind, rel, args: change(args) if rel.endswith(rel_suffix) else args


def scenario_text_hygiene(repository):
    def code(path, marker):
        text = (repository / path).read_text()
        return "\n".join(line.split(marker, 1)[0] for line in text.splitlines())

    helper = code("cmake/SobolIdentity.cmake", "#") + code("cmake/GenerateSobolIdentity.cmake", "#")
    helper += code("cmake/CompileCommandBinding.cmake", "#")
    for forbidden in ("target_compile_options", "target_compile_definitions", "add_compile_options",
                      "add_definitions", "set_target_properties", "target_link_options",
                      "target_include_directories", "set(CMAKE_CXX_FLAGS", "set(CMAKE_C_FLAGS",
                      "numeric_build_flags", "GenerateNumericBuildFlags", "PFH_NUMERIC_BUILD",
                      "CMAKE_CXX_FLAGS_", "CMAKE_C_FLAGS_", "ReturnStats", "return_stats",
                      "TARGET_PROPERTY:${SI_TARGET},COMPILE_OPTIONS"):
        require(forbidden not in helper, f"the helpers must not use or model '{forbidden}'")
    runtime = code("src/core/sobol_identity.cpp", "//")
    for pattern in (r"(?<![A-Za-z_])numeric_build_identity\(", r"tpe_numeric_identity",
                    r"PFH_NUMERIC_BUILD", r"flags_sha256", r"kTpeAlgorithmRevision",
                    r"return_stats"):
        require(not re.search(pattern, runtime), f"sobol_identity.cpp must not use '{pattern}'")
    require(runtime.index("detail::require_portable_environment();")
            < runtime.index("if (!generated::kBound)"),
            "the portable environment must be checked before the unbound refusal")
    header = code("include/pineforge/hpo/sobol_identity.hpp", "//")
    for forbidden in ("generated", "numeric_build.hpp", "tpe_", "checkpoint", "dlib"):
        require(forbidden not in header.lower(), f"the public header must not mention '{forbidden}'")
    note = (repository / "docs/internal/sobol-identity.md").read_text()
    require(not re.search(r"(^|[\s`\"'(=])/(Users|home|private|tmp|var|opt|mnt|root)/", note, re.M),
            "the note must not contain an absolute private path")
    require(repository.as_posix() not in note, "the note must not contain the repository path")
    for word in ("spot", "EC2", "AWS", "supervisor", "executor", ".executors", "Claude"):
        require(word not in note, f"the note must not mention '{word}'")
    print("PASS: helpers model no flags and touch no target-wide property; runtime refuses in order")


INCLUDE_LINE = re.compile(r'(?m)^\s*#\s*include\s+[<"]([^>"]+)[>"]')


def static_closure(repository, start):
    """Project files reachable from `start` through #include lines, from text alone.

    It needs no compiler and no build: it answers which shipped translation units can emit a copy
    of a shared inline helper. Angle-bracket and quoted names resolve against the including file's
    directory and the project include roots; anything else is a system header."""
    roots = [repository / name for name in ("include", "src/core", "src/cli")]
    seen, stack = set(), [repository / start]
    while stack:
        path = Path(os.path.normpath(stack.pop()))
        if path in seen:
            continue
        seen.add(path)
        for name in INCLUDE_LINE.findall(path.read_text(errors="replace")):
            for candidate in [path.parent / name, *(root / name for root in roots)]:
                candidate = Path(os.path.normpath(candidate))
                if candidate.is_file() and repository in candidate.parents:
                    stack.append(candidate)
                    break
    return {path.relative_to(repository).as_posix() for path in seen}


def scenario_providers_complete(repository):
    """Every shipped unit that can emit a shared inline helper is a Sobol unit or a provider."""
    cxx_rel, _ = helper_inventory(repository)
    providers = set(provider_inventory(repository))
    covered = set(cxx_rel) | providers
    shipped = sorted(path.relative_to(repository).as_posix()
                     for pattern in ("src/core/*.cpp", "src/engine_adapter/*.cpp", "src/cli/*.cpp")
                     for path in repository.glob(pattern))
    require("src/core/tpe_sampler.cpp" in shipped and "src/cli/main.cpp" in shipped,
            "the shipped translation-unit scan found too little")
    for unit in shipped:
        reaches = sorted(set(SHARED_HELPER_HEADERS) & static_closure(repository, unit))
        if reaches:
            require(unit in covered,
                    f"{unit} includes {reaches}, so it can emit a shared inline helper, but it is "
                    "neither a Sobol unit nor a listed provider in cmake/SobolIdentity.cmake")
    for provider in sorted(providers):
        require(set(SHARED_HELPER_HEADERS) & static_closure(repository, provider),
                f"{provider} is listed as a provider but reaches no shared helper header")
    # The providers carry the strict recipe in the root, so a regression there is also visible.
    root_text = (repository / "CMakeLists.txt").read_text()
    for provider in sorted(providers):
        require(provider in root_text, f"the root does not mention the provider {provider}")
    print(f"PASS: {len(shipped)} shipped translation units scanned; the shared-helper providers "
          f"are exactly {sorted(providers)}")


def scenario_synthetic(base):
    world = World(base, "w-base")
    first = world.generate()
    require(first["bound"], f"baseline is unbound: {first['reason']}")
    emitted, digest = check_outputs(world.out, first["ingredients"], first["database"])
    require(emitted.splitlines()[0] == FORMAT, "unexpected descriptor header")
    stamps = {p.name: p.stat().st_mtime_ns for p in world.out.iterdir()}
    require(world.generate()["digest"] == digest, "an unchanged rerun changed the digest")
    require(stamps == {p.name: p.stat().st_mtime_ns for p in world.out.iterdir()},
            "an unchanged rerun rewrote its outputs")

    # Sensitivity: every perturbation, in either language group, moves the digest.
    perturbations = {
        "cxx-fast-math": only("sobol_mapper.cpp", lambda a: a + ["-ffast-math"]),
        "cxx-contract-removed": only("sobol_sampler.cpp",
                                     lambda a: [t for t in a if t != "-ffp-contract=off"]),
        "cxx-order": only("sobol_engine.cpp", lambda a: [
            "-ffp-contract=off" if t == "-fno-fast-math" else
            "-fno-fast-math" if t == "-ffp-contract=off" else t for t in a]),
        "cxx-definition": only("sobol_identity.cpp", lambda a: a + ["-DPFH_B=1"]),
        "cxx-optimization": only("sobol_mapper.cpp", lambda a: [
            "-O3" if t == "-O2" else t for t in a]),
        "cxx-fma-target": only("sobol_mapper.cpp", lambda a: a + ["-mfma"]),
        "c-flag-added": only("/log.c", lambda a: a + ["-fno-trapping-math"]),
        "c-contract-removed": only("exp.c", lambda a: [t for t in a if t != "-ffp-contract=off"]),
        "c-canary-unit": only("portable_math_canary.c", lambda a: a + ["-mfma"]),
        "c-standard": only("log.c", lambda a: ["-std=c11" if t == "-std=gnu11" else t for t in a]),
        # The shared-helper providers: a change of their generated command moves the identity too.
        "provider-flag-added": only("tpe_sampler.cpp", lambda a: a + ["-fno-trapping-math"]),
        "provider-contract-removed": only("search_space.cpp",
                                          lambda a: [t for t in a if t != "-ffp-contract=off"]),
        "provider-definition": only("tpe_sampler.cpp", lambda a: a + ["-DPFH_B=1"]),
        "provider-optimization": only("search_space.cpp",
                                      lambda a: ["-O3" if t == "-O2" else t for t in a]),
    }
    digests = {"baseline": digest}
    for name, mutate in perturbations.items():
        variant = World(base, f"w-{name}")
        result = variant.generate(mutate=mutate)
        require(result["bound"], f"{name}: unbound ({result['reason']})")
        check_outputs(variant.out, result["ingredients"], result["database"])
        digests[name] = result["digest"]
    require(len(set(digests.values())) == len(digests), f"collision: {digests}")

    # Content binding: sources, headers, the table, the forced include of the C commands, drivers.
    edit = World(base, "w-edit")
    original = edit.generate()["digest"]
    require(original == digest, "an identical world in another place differs")
    for rel in (edit.cxx_rel[0], edit.cxx_rel[2], World.C_SOURCES[1], World.C_SOURCES[2],
                *edit.provider_rel,
                "src/core/sobol_mapper.hpp", "src/core/sobol_table_joe_kuo_d6_1024.inc",
                "src/core/portable_grid.hpp", "src/core/numeric_build.hpp",
                "third_party/core_math/log/dint.h", "third_party/core_math/portable.h"):
        path = edit.root / rel
        text = path.read_text()
        path.write_text(text + "// edited\n")
        require(edit.generate()["digest"] != original, f"editing {rel} kept the digest")
        path.write_text(text)
        require(edit.generate()["digest"] == original, f"restoring {rel} did not restore")
    (edit.root / "include/unlisted.hpp").parent.mkdir(exist_ok=True)
    (edit.root / "include/unlisted.hpp").write_text("// not bound\n")
    require(edit.generate()["digest"] == original, "an unlisted file moved the digest")
    edit.cxx.write_bytes(b"FAKE-CXX-2\n")
    require(edit.generate()["digest"] != original, "changed C++ driver bytes kept the digest")
    edit.cxx.write_bytes(b"FAKE-CXX-1\n")
    edit.cc.write_bytes(b"FAKE-CC-2\n")
    require(edit.generate()["digest"] != original, "changed C driver bytes kept the digest")
    edit.cc.write_bytes(b"FAKE-CC-1\n")
    require(edit.generate(**{"c.version": "13.2.1"})["digest"] != original, "C version ignored")
    require(edit.generate(**{"cxx.version": "13.2.1"})["digest"] != original, "C++ version ignored")
    require(edit.generate(configuration="Debug")["digest"] != original, "configuration ignored")
    require(edit.generate(contract="portable-sobol-v2")["reason"] == "contract_mismatch",
            "a contract mismatch was not refused")

    # Declared normalization: moved trees, object and dependency names, compiler aliases.
    require(World(base, "w-moved-with-a-longer-name").generate()["digest"] == digest,
            "a moved tree changed the digest")

    def rename(kind, rel, args):
        args = list(args)
        args[args.index("-MD")] = "-MMD"
        args[args.index("-MT") + 1] = "other/object-name.o"
        args[args.index("-MF") + 1] = "other/object-name.dep"
        owner = "x_math" if kind == "c" else "x_core"
        args[args.index("-o") + 1] = f"CMakeFiles/{owner}.dir/z.o"
        return args

    require(World(base, "w-noop").generate(mutate=rename)["digest"] == digest,
            "object or dependency-file names changed the digest")
    alias = World(base, "w-alias")
    (alias.root / "tools/cxx-alias").write_bytes(alias.cxx.read_bytes())
    (alias.root / "tools/cc-link").symlink_to(alias.cc)

    def aliased(kind, rel, args):
        args = list(args)
        args[0] = (alias.root / ("tools/cc-link" if kind == "c" else "tools/cxx-alias")).as_posix()
        return args

    result = alias.generate(mutate=aliased, **{
        "cxx.path": (alias.root / "tools/cxx-alias").as_posix(),
        "c.path": (alias.root / "tools/cc-link").as_posix()})
    require(result["digest"] == digest, "compiler copies and symlinks with equal bytes differ")
    print(f"PASS: {len(digests)} flag/definition/order variants, content edits and driver bytes "
          f"move the digest; moves, renames and aliases do not")


def scenario_fail_closed(base):
    def tokens(*extra, kind="cxx"):
        def build(world):
            root = world.root.as_posix()
            entries = []
            for k, rels in world.groups():
                for rel in rels:
                    args = world.args(k, rel)
                    if k == kind and rel == rels[0]:
                        args[-1:-1] = list(extra)
                    entries.append({"directory": f"{root}/build", "command": shlex.join(args),
                                    "file": f"{root}/{rel}"})
            return entries
        return build

    def missing_c(world):
        return [e for e in tokens()(world) if not e["file"].endswith("exp.c")]

    def duplicate(world):
        entries = tokens()(world)
        return entries + [dict(entries[0])]

    def wrong_compiler(world):
        entries = tokens()(world)
        for entry in entries:
            if entry["file"].endswith("log.c"):
                entry["command"] = entry["command"].replace(
                    world.cc.as_posix(), world.cxx.as_posix(), 1)
        return entries

    def missing_provider(world):
        return [e for e in tokens()(world) if not e["file"].endswith("tpe_sampler.cpp")]

    def provider_wrong_compiler(world):
        entries = tokens()(world)
        for entry in entries:
            if entry["file"].endswith("search_space.cpp"):
                entry["command"] = entry["command"].replace(
                    world.cxx.as_posix(), world.cc.as_posix(), 1)
        return entries

    cases = [
        ("multi_config_generator", {"multi_config": "ON"}),
        ("generator_without_compile_database", {"generator": "Xcode"}),
        ("generator_without_compile_database", {"generator": "Ninja Multi-Config"}),
        ("compile_database_disabled", {"database_enabled": "OFF"}),
        ("compiler_launcher", {"launcher.core": "ccache"}),
        ("compiler_launcher", {"launcher.math": "ccache"}),
        ("compiler_launcher", {"rule_launch.global": "ccache"}),
        ("compiler_launcher", {"rule_launch.directory": "ccache"}),
        ("compiler_launcher", {"rule_launch.core": "ccache"}),
        ("compiler_launcher", {"rule_launch.math": "ccache"}),
        ("compiler_arguments", {"cxx.arg1": "--driver-mode=g++"}),
        ("compiler_arguments", {"c.arg1": "--driver-mode=gcc"}),
        ("compiler_unreadable", {"cxx.path": "/nonexistent/cxx"}),
        ("compiler_unreadable", {"c.path": "/nonexistent/cc"}),
        ("compile_database_missing", {"entries": "none"}),
        ("compile_database_unreadable", {"entries": []}),
        ("compile_command_missing", {"entries": missing_c}),
        ("compile_command_ambiguous", {"entries": duplicate}),
        ("compiler_mismatch", {"entries": wrong_compiler}),
        ("response_file", {"entries": tokens("@flags.rsp")}),
        ("response_file", {"entries": tokens("@flags.rsp", kind="c")}),
        ("unsupported_character", {"entries": tokens("-DPFH_X=a;b")}),
        ("unsupported_character", {"entries": tokens("-DPFH_X=[1]", kind="c")}),
        ("forced_include_unresolved", {"entries": tokens("-include", "missing-forced.h",
                                                         kind="c")}),
        ("forced_include_unresolved", {"entries": tokens("-include/nonexistent/forced.h",
                                                         kind="c")}),
        ("forced_include_unresolved", {"entries": tokens("-imacros/nonexistent/macros.h")}),
        ("ingredients_incomplete", {"drop": ("core.target",)}),
        ("ingredients_incomplete", {"drop": ("c.path",)}),
        ("contract_mismatch", {"contract": "portable-sobol-v9"}),
        ("compiler_is_script", {"script": "cxx"}),
        ("compiler_is_script", {"script": "cc"}),
        ("reducer_file_missing", {"missing_header": True}),
        # The shared-helper providers fail closed like every other bound unit.
        ("compile_command_missing", {"entries": missing_provider}),
        ("compiler_mismatch", {"entries": provider_wrong_compiler}),
        ("response_file", {"entries": tokens("@flags.rsp", kind="provider")}),
        ("unsupported_character", {"entries": tokens("-DPFH_X=a;b", kind="provider")}),
        ("forced_include_unresolved", {"entries": tokens("-include/nonexistent/forced.h",
                                                         kind="provider")}),
        ("ingredients_incomplete", {"providers": []}),
        ("reducer_file_missing", {"missing_provider_file": True}),
    ]
    for number, (reason, spec) in enumerate(cases):
        world = World(base, f"w-closed-{number}")
        entries, drop, headers, overrides, providers = None, (), None, {}, None
        for key, value in spec.items():
            if key == "entries":
                entries = value(world) if callable(value) else value
            elif key == "providers":
                providers = value
            elif key == "missing_provider_file":
                (world.root / world.provider_rel[0]).unlink()
            elif key == "drop":
                drop = value
            elif key == "script":
                (world.cxx if value == "cxx" else world.cc).write_bytes(b"#!/bin/sh\nexec c++ \"$@\"\n")
            elif key == "missing_header":
                headers = [*world.headers_rel, "include/missing.hpp"]
            else:
                overrides[key] = value
        result = world.generate(entries=entries, drop=drop, headers=headers, providers=providers,
                                **overrides)
        require(not result["bound"], f"{reason} #{number}: the build was bound")
        require(result["reason"] == reason, f"#{number}: reason {result['reason']}, wanted {reason}")
        require(result["digest"] == "", f"{reason}: digest not empty")
        require(f"reason={reason}" in result["descriptor"], f"{reason}: descriptor lacks it")
        require('kBuildDigest[] = "";' in result["header"], f"{reason}: header carries a digest")
    print(f"PASS: {len(cases)} unbindable situations fail closed with an exact reason")


def scenario_same_rule_as_x(base):
    """The shared rule must equal the return-statistics generator's on one database entry."""
    X.REPOSITORY = REPOSITORY
    x_world = X.World(base, "x-equivalence")
    x_result = x_world.generate()
    require(x_result["bound"], "the return-statistics baseline is unbound")
    x_lines = [l for l in x_result["descriptor"].splitlines() if l.startswith("arg ")]
    sobol = World(base, "w-equivalence")

    def x_shaped(kind, rel, args):
        """The return-statistics fixture's exact command, moved onto one Sobol source."""
        if kind != "cxx" or not rel.endswith("sobol_engine.cpp"):
            return args
        moved = [t.replace(x_world.root.as_posix(), sobol.root.as_posix())
                  .replace("x_core.dir/src/reducer.cpp", f"x_core.dir/{rel}")
                  .replace("src/reducer.cpp", rel) for t in x_world.arguments()]
        moved[0] = sobol.cxx.as_posix()
        return moved

    result = sobol.generate(mutate=x_shaped)
    require(result["bound"], f"the equivalence world is unbound: {result['reason']}")
    block = [b for b in blocks_of(result["descriptor"])
             if b.splitlines()[0].endswith("sobol_engine.cpp")][0]
    s_lines = [l for l in block.splitlines() if l.startswith("arg ")]
    # Only the last line (the source file) differs by name; everything else must be identical.
    require(x_lines[:-1] == s_lines[:-1],
            f"the two generators normalize one command differently:\n{x_lines}\n{s_lines}")
    require("arg -isystem" in s_lines and any(l.startswith("arg -I<src>/") for l in s_lines),
            "path-valued options were not rewritten")
    print("PASS: the shared normalization equals the return-statistics generator's on one command")


# ---------------------------------------------------------------------------------------------
# Configured projects: the real compilation database of a real build system.
# ---------------------------------------------------------------------------------------------


def configure(source, build, *definitions, generator=None, build_type=CONFIGURATION):
    command = ["cmake"] + (["-G", generator] if generator else [])
    command += ["-S", source, "-B", build, f"-DCMAKE_BUILD_TYPE={build_type}", *definitions]
    return run(command)


def build_target(build, target, *tool_arguments):
    command = ["cmake", "--build", build, "--target", target]
    if tool_arguments:
        command += ["--", *tool_arguments]
    return run(command)


def outputs(build, build_type=CONFIGURATION):
    return build / GENERATED / build_type


def read_database(build):
    return json.loads((build / "compile_commands.json").read_text())


def identity_of(build, target, *definitions, generator=None, build_type=CONFIGURATION, source=None):
    configure(source, build, *definitions, generator=generator, build_type=build_type)
    build_target(build, f"{target}_sobol_identity")
    directory = outputs(build, build_type)
    return check_outputs(directory, directory / INGREDIENTS, read_database(build))


def write_fixture(directory):
    (directory / "src/core").mkdir(parents=True)
    (directory / "math").mkdir()
    (directory / "CMakeLists.txt").write_text(FIXTURE_CMAKE.replace("@REPOSITORY@", REPOSITORY.as_posix()))
    cxx_rel, headers_rel = helper_inventory(REPOSITORY)
    for rel in cxx_rel + provider_inventory(REPOSITORY):
        body = FIXTURE_IDENTITY_CPP if rel.endswith("sobol_identity.cpp") else \
            f'extern "C" int pfh_fixture_{Path(rel).stem}() {{ return 1; }}\n'
        (directory / rel).write_text(body)
    for rel in headers_rel:
        path = directory / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"// {rel}\n")
    for number in (1, 2):
        (directory / f"math/m{number}.c").write_text(FIXTURE_MATH_C.format(n=number))
    (directory / "probe.cpp").write_text(FIXTURE_PROBE_CPP)


def scenario_fixture(base, generator):
    tag = generator.replace(" ", "-")
    source = base / f"fixture-{tag}"
    write_fixture(source)
    descriptor, digest = identity_of(base / f"fx-{tag}-a", "x_core", generator=generator, source=source)
    require_strict(descriptor, minimum_c=2)
    for flag in ("-DNDEBUG", "-std=gnu11", "-frounding-math"):
        require(f"arg {flag}\n" in descriptor, f"{flag} missing from the bound commands")
    # Ordering and the per-source include directory: the consumer built and reports the digest.
    probe_build = base / f"fx-{tag}-a"
    build_target(probe_build, "x_probe")
    reported = run([probe_build / "x_probe"]).stdout.strip()
    require(reported == digest, "the compiled digest differs from the retained file")
    require("arg -I<build>/generated/sobol_identity/Release\n" in descriptor,
            "the per-source include directory is not in the identity unit's command")
    require(descriptor.count("generated/sobol_identity") == 1,
            "the generated directory leaked into more than the identity unit's command")
    _, again = identity_of(base / f"fx-{tag}-b", "x_core", generator=generator, source=source)
    require(again == digest, "the digest depends on the build directory")
    moved = base / f"fixture-{tag}-moved"
    shutil.copytree(source, moved)
    _, moved_digest = identity_of(base / f"fx-{tag}-moved", "x_core", generator=generator,
                                  source=moved)
    require(moved_digest == digest, "the digest depends on where the source tree lives")

    variants = {
        "dependency-usage-requirement": ["-DPFH_TEST_DEPENDENCY=ON"],
        "late-source-property": ["-DPFH_TEST_LATE_SOURCE_OPTION=-fno-trapping-math"],
        "late-math-target-option": ["-DPFH_TEST_LATE_MATH_OPTION=-fno-signed-zeros"],
        "late-cxx-flags": ["-DPFH_TEST_LATE_CXX_FLAGS=-fno-associative-math"],
        "late-c-flags": ["-DPFH_TEST_LATE_C_FLAGS=-fno-associative-math"],
        # A provider's own late per-source option: it reaches the provider block only.
        "late-provider-option": ["-DPFH_TEST_LATE_PROVIDER_OPTION=-fno-trapping-math"],
    }
    expected = {
        "dependency-usage-requirement": ["-fno-strict-aliasing", "-DPFH_X_DEPENDENCY_PROVIDED=1"],
        "late-source-property": ["-fno-trapping-math"],
        "late-math-target-option": ["-fno-signed-zeros"],
        "late-cxx-flags": ["-fno-associative-math"],
        "late-c-flags": ["-fno-associative-math"],
        "late-provider-option": ["-fno-trapping-math"],
    }
    digests = {"baseline": digest}
    for name, definitions in variants.items():
        variant_descriptor, result = identity_of(
            base / f"fx-{tag}-{name}", "x_core", *definitions, generator=generator, source=source)
        for token in expected[name]:
            require(f"arg {token}\n" in variant_descriptor, f"{name}: {token} is not bound")
        if name == "late-provider-option":
            blocks = {b.splitlines()[0]: b for b in blocks_of(variant_descriptor)}
            hit = blocks["command provider <src>/src/core/tpe_sampler.cpp"]
            require("arg -fno-trapping-math\n" in hit, "the provider block lacks its late option")
            require(not any("arg -fno-trapping-math\n" in b for head, b in blocks.items()
                            if head != "command provider <src>/src/core/tpe_sampler.cpp"),
                    "a provider's late option leaked into another unit's command")
        digests[name] = result
    for build_type, flags in (("Debug", []),
                              ("FeedAudit", ["-DCMAKE_C_FLAGS_FEEDAUDIT=-O1"]),
                              ("FeedAudit2", ["-DCMAKE_C_FLAGS_FEEDAUDIT2=-O2"])):
        _, result = identity_of(base / f"fx-{tag}-{build_type}", "x_core", *flags,
                                generator=generator, build_type=build_type, source=source)
        digests[f"configuration-{build_type}"] = result
    require(len(set(digests.values())) == len(digests), f"collision: {digests}")
    print(f"PASS [{generator}]: dependency, late-property, late-flag (C++ and C), math-target and "
          f"configuration changes move the digest ({len(digests)} distinct); the consumer reports it")


def executed_command(build, target, source_path, generator):
    """The compile command a verbose rebuild executes for one source, as printed by the tool."""
    obj_hint = Path(source_path).name + ".o"
    for path in build.rglob(obj_hint):
        path.unlink()
    process = build_target(build, target, "-v" if generator == "Ninja" else "VERBOSE=1")
    log = process.stdout + process.stderr
    found = sorted({
        re.sub(r"^\[\d+/\d+\]\s+", "", part.strip())
        for line in log.splitlines() if " -c " in line and source_path in line
        for part in line.split("&&") if " -c " in part and source_path in part})
    require(len(found) == 1, f"expected one executed command for {source_path}: {found}")
    return found[0]


def scenario_executed(base, generator):
    tag = generator.replace(" ", "-")
    source = base / f"executed-{tag}"
    write_fixture(source)
    build = base / f"ex-{tag}"
    configure(source, build, "-DPFH_TEST_DEPENDENCY=ON", generator=generator)
    database = read_database(build)
    roots = (os.path.normpath(source), os.path.normpath(build))
    for target, rel in (("x_core", "src/core/sobol_mapper.cpp"), ("x_math", "math/m1.c")):
        path = os.path.normpath(source / rel)
        entry = [e for e in database if os.path.normpath(e["file"]) == path
                 and f"/{target}.dir/" in e["command"]][0]
        executed = dict(entry, command=executed_command(build, target, path, generator))
        require(normalize(executed, path, *roots)[1] == normalize(entry, path, *roots)[1],
                f"the executed command of {rel} differs from the compilation database entry")
    print(f"PASS [{generator}]: executed C++ and C commands equal the database entries")


def fixture_declarations(source):
    """What the fixture's own build file declares for the helper call, read back from that file."""
    text = (source / "CMakeLists.txt").read_text()
    call = re.search(r"pfh_sobol_identity\(([^)]*)\)", text).group(1)
    keyword = lambda name: re.search(rf"\b{name}\s+(\S+)", call).group(1)
    math = keyword("PORTABLE_MATH_TARGET")
    math_files = re.search(rf"add_library\({re.escape(math)}\s+STATIC\s+([^)]*)\)", text).group(1)
    return {
        "core": keyword("TARGET"),
        "math": math,
        "math_files": math_files.split(),
        "other": re.search(r"add_executable\((\S+)", text).group(1),
    }


def scenario_ingredients(base, generator):
    """Configure only: the ingredients file carries the helper call and the finished directory.

    The writer runs at the end of the top-level directory, so it has to receive its arguments
    from the call site, and it has to read the launcher state that the project sets after the
    call. Every expected value below comes from what this test wrote into the fixture or from the
    helper's own inventory lists, never from a previous output.
    """
    tag = generator.replace(" ", "-")
    source = base / f"ingredients-{tag}"
    write_fixture(source)
    declared = fixture_declarations(source)
    core, math, other = declared["core"], declared["math"], declared["other"]
    require(len({core, math, other}) == 3, "the fixture needs three distinct targets")

    # Launcher state set after the helper call. The third target gets values of its own, so a
    # launcher in the file can only have been read from the declared targets.
    expected = {
        "launcher.core": "pfh-test-core-launcher",
        "launcher.math": "pfh-test-math-launcher",
        "rule_launch.core": "pfh-test-core-rule-launch",
        "rule_launch.math": "pfh-test-math-rule-launch",
        "rule_launch.global": "pfh-test-global-rule-launch",
        "rule_launch.directory": "pfh-test-directory-rule-launch",
    }
    other_values = ("pfh-test-other-cxx-launcher", "pfh-test-other-c-launcher",
                    "pfh-test-other-rule-launch")
    properties = [
        (f"TARGET {core}", "CXX_COMPILER_LAUNCHER", expected["launcher.core"]),
        (f"TARGET {math}", "C_COMPILER_LAUNCHER", expected["launcher.math"]),
        (f"TARGET {core}", "RULE_LAUNCH_COMPILE", expected["rule_launch.core"]),
        (f"TARGET {math}", "RULE_LAUNCH_COMPILE", expected["rule_launch.math"]),
        ("GLOBAL", "RULE_LAUNCH_COMPILE", expected["rule_launch.global"]),
        ("DIRECTORY", "RULE_LAUNCH_COMPILE", expected["rule_launch.directory"]),
        (f"TARGET {other}", "CXX_COMPILER_LAUNCHER", other_values[0]),
        (f"TARGET {other}", "C_COMPILER_LAUNCHER", other_values[1]),
        (f"TARGET {other}", "RULE_LAUNCH_COMPILE", other_values[2]),
    ]
    with open(source / "CMakeLists.txt", "a") as handle:
        for scope, name, value in properties:
            handle.write(f"set_property({scope} PROPERTY {name} {value})\n")

    build = base / f"ingredients-build-{tag}"
    configure(source, build, generator=generator)
    path = outputs(build) / INGREDIENTS
    require(path.is_file(), f"[{generator}] the ingredients file was not written: {path}")
    text = path.read_text()
    require(text.strip(), f"[{generator}] the ingredients file is empty")
    require("$<" not in text, f"[{generator}] a generator expression was left unevaluated:\n{text}")
    values, cxx, c_files, headers, providers = parse_ingredients(path)
    missing = sorted(set(World(base, f"keys-{tag}").ingredients()) - set(values))
    require(not missing, f"[{generator}] ingredient lines missing: {missing}\n{text}")

    wanted = {
        "core.target": core,
        "math.target": math,
        "contract": CONTRACT,
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
    cxx_rel, headers_rel = helper_inventory(REPOSITORY)
    for label, found, relative in (("source.cxx", cxx, cxx_rel),
                                   ("source.provider", providers, provider_inventory(REPOSITORY)),
                                   ("header", headers, headers_rel),
                                   ("source.c", c_files, declared["math_files"])):
        require([real(item) for item in found] == [real(source / item) for item in relative],
                f"[{generator}] the {label} lines differ from the declared files: {found}")
    require(not any(value in text for value in other_values),
            f"[{generator}] a launcher of another target was bound:\n{text}")
    print(f"PASS [{generator}]: the ingredients file holds the declared targets, files and the "
          f"launcher state set after the call")


def ingredients_group(base, generators):
    for generator in generators:
        scenario_ingredients(base, generator)


def scenario_not_circular():
    cxx_rel, headers_rel = helper_inventory(REPOSITORY)
    for rel in cxx_rel + headers_rel:
        require("sobol_identity_generated" not in rel, f"{rel}: the generated header is an input")
        require(not rel.startswith("build"), f"{rel}: a build-tree file is an input")
    helper = (REPOSITORY / "cmake/SobolIdentity.cmake").read_text()
    require("sobol_identity_generated" in helper and "circular" in helper,
            "the helper lacks its circularity guard")
    print("PASS: the generated header is not in the inventory and the helper guards it")


# ---------------------------------------------------------------------------------------------
# Copies of the real project.
# ---------------------------------------------------------------------------------------------


def copy_tree(repository, destination, text_edit=None):
    destination.mkdir(parents=True)
    for name in ("CMakeLists.txt", "VERSION", "cmake", "include", "src", "third_party"):
        origin = repository / name
        if origin.is_dir():
            shutil.copytree(origin, destination / name)
        else:
            shutil.copy2(origin, destination / name)
    if text_edit:
        text_edit(destination)


def strip_block(destination):
    path = destination / "CMakeLists.txt"
    text = path.read_text()
    begin, end = text.find(MARK_BEGIN), text.find(MARK_END)
    require(begin != -1 and end > begin, "the Sobol integration block is not delimited")
    path.write_text(text[:begin] + text[end + len(MARK_END):])


def include_closure(entry, repository_root):
    """Project files that the compiler reports for one bound command (preprocess only)."""
    tokens, skip, kept = shlex.split(entry["command"]), False, []
    for token in tokens[1:]:
        if skip:
            skip = False
        elif token in X.DROP_WITH_ARGUMENT:
            skip = True
        elif token not in X.DROP_FLAGS and token != "-c":
            kept.append(token)
    process = subprocess.run([tokens[0], *kept, "-MM", "-MG", entry["file"]],
                             cwd=entry["directory"], capture_output=True, text=True, timeout=300)
    require(process.returncode == 0, f"dependency scan failed:\n{process.stderr}")
    text = process.stdout.replace("\\\n", " ")
    names = re.sub(r"\\ ", "\0", text.split(":", 1)[1]).split()
    paths = {os.path.normpath(os.path.join(entry["directory"], n.replace("\0", " "))) for n in names}
    return {p for p in paths if p.startswith(os.path.normpath(repository_root) + "/")}


def scenario_real_core(repository, dlib, base, generator):
    root_text = (repository / "CMakeLists.txt").read_text()
    integrated = "pfh_sobol_identity(" in root_text
    definitions = ("-DPINEFORGE_HPO_BUILD_TESTS=OFF", "-DPINEFORGE_HPO_BUILD_NATIVE_CLI=OFF",
                   "-DPINEFORGE_HPO_BUILD_ENGINE_ADAPTER=OFF",
                   f"-DFETCHCONTENT_SOURCE_DIR_DLIB={dlib}")
    has_x = "pfh_return_stats_identity(" in root_text

    def with_sobol(destination):
        if not integrated:
            with open(destination / "CMakeLists.txt", "a") as handle:
                handle.write(SOBOL_INTEGRATION)

    def without_sobol(destination):
        if integrated:
            strip_block(destination)

    trees = {"baseline": without_sobol, "sobol": with_sobol}
    builds = {}
    for label, edit in trees.items():
        source = base / f"core-{label}"
        copy_tree(repository, source, edit)
        build = base / f"core-build-{label}"
        configure(source, build, *definitions, generator=generator)
        build_target(build, "pineforge_hpo_numeric_flags")
        if has_x:
            build_target(build, "pineforge_hpo_core_return_stats_identity")
        builds[label] = (source, build)

    def foreign_bytes_of(build, label):
        """The TPE flag files and the return-statistics outputs of one configured tree."""
        build_target(build, "pineforge_hpo_numeric_flags")
        if has_x:
            build_target(build, "pineforge_hpo_core_return_stats_identity")
        files = [build / "generated" / CONFIGURATION / "CXX" / n
                 for n in ("numeric_build_flags.txt", "numeric_build_flags.hpp")]
        if has_x:
            files += [outputs(build).parent.parent / "return_stats_identity" / CONFIGURATION / n
                      for n in ("return_stats_identity.descriptor.txt", "return_stats_identity.txt")]
        for path in files:
            require(path.is_file(), f"{path.name} was not generated in the {label} tree")
        return tuple(path.read_bytes() for path in files)

    def foreign_bytes(label):
        return foreign_bytes_of(builds[label][1], label)

    baseline_foreign = foreign_bytes("baseline")
    require(foreign_bytes("sobol") == baseline_foreign,
            "adding Sobol changed the TPE flag bytes or the return-statistics descriptor/identity")
    require(not (builds["baseline"][1] / GENERATED).exists(), "the baseline carries Sobol outputs")

    source, build = builds["sobol"]
    target = "pineforge_hpo_core"
    descriptor, digest = identity_of(build, target, *definitions, generator=generator, source=source)
    require_strict(descriptor, minimum_c=7)
    require("arg -include<src>/third_party/core_math/portable.h#sha256=" in descriptor,
            "the joined forced include of the math target is not normalized")
    require(source.as_posix() not in descriptor and build.as_posix() not in descriptor,
            "the descriptor of the real project contains its own location")
    relocated = base / "deeper-tree" / "copy"
    copy_tree(repository, relocated, with_sobol)
    _, relocated_digest = identity_of(base / "deeper-build" / "b", target, *definitions,
                                      generator=generator, source=relocated)
    require(relocated_digest == digest, "the identity of the real project depends on its location")
    database = read_database(build)
    bound = {os.path.normpath(source / m.group(1)) for m in re.finditer(
        r"(?m)^(?:source|header) <src>/(\S+) sha256=", descriptor)}
    bound_units = {os.path.normpath(source / m.group(1)) for m in re.finditer(
        r"(?m)^command (?:cxx|provider) <src>/(\S+)$", descriptor)}
    shared = {os.path.normpath(source / header) for header in SHARED_HELPER_HEADERS}
    for entry in database:
        in_core = f"/{target}.dir/" in entry["command"]
        sobol_unit = in_core and Path(entry["file"]).name.startswith("sobol_")
        math_unit = "/pineforge_hpo_portable_math.dir/" in entry["command"]
        if sobol_unit or math_unit:
            closure = include_closure(entry, source)
            missing = sorted(p for p in closure if p not in bound)
            require(not missing, f"headers included but not bound for {entry['file']}: {missing}")
        elif in_core and entry["file"].endswith(".cpp"):
            # Any other core unit that can emit a shared inline helper must be a bound provider.
            if include_closure(entry, source) & shared:
                require(os.path.normpath(entry["file"]) in bound_units,
                        f"{entry['file']} can emit a shared inline helper but is not bound")
    build_target(build, target)
    roots = (os.path.normpath(source), os.path.normpath(build))
    # The mapper, both shared-helper providers (their actual commands are what the linker's copy
    # of a shared inline helper was compiled with) and one math C unit.
    for tgt, rel in ((target, "src/core/sobol_mapper.cpp"),
                     *((target, provider) for provider in provider_inventory(repository)),
                     ("pineforge_hpo_portable_math", "third_party/core_math/log/log.c")):
        path = os.path.normpath(source / rel)
        entry = [e for e in database if os.path.normpath(e["file"]) == path
                 and f"/{tgt}.dir/" in e["command"]][0]
        executed = dict(entry, command=executed_command(build, tgt, path, generator))
        require(normalize(executed, path, *roots)[1] == normalize(entry, path, *roots)[1],
                f"the executed command of {rel} differs from the database entry")

    # Perturbations of the real combined target: each must move the Sobol digest.
    moved = {}
    for name, extra in (("c-flags-only", ["-DCMAKE_C_FLAGS=-fno-strict-aliasing"]),
                        ("cxx-flags", ["-DCMAKE_CXX_FLAGS=-fno-strict-aliasing"]),
                        ("sanitizer", ["-DPINEFORGE_HPO_ENABLE_SANITIZERS=ON"])):
        tree = base / f"core-{name}"
        copy_tree(repository, tree, with_sobol)
        _, moved[name] = identity_of(base / f"core-build-{name}", target, *definitions, *extra,
                                     generator=generator, source=tree)
    for name, rel, text in (("math-source", "third_party/core_math/log/log.c", "// edited\n"),
                            ("mapper-header", "src/core/sobol_mapper.hpp", "// edited\n"),
                            ("table", "src/core/sobol_table_joe_kuo_d6_1024.inc", "// edited\n"),
                            ("canary-source", "src/core/portable_math_canary.c", "// edited\n"),
                            # The shared-helper providers: a source edit moves the Sobol digest
                            # and leaves the TPE flag files and the X outputs byte-identical.
                            ("provider-source-tpe", "src/core/tpe_sampler.cpp", "// edited\n"),
                            ("provider-source-grid", "src/core/search_space.cpp", "// edited\n")):
        tree = base / f"core-{name}"
        copy_tree(repository, tree, with_sobol)
        (tree / rel).write_text((tree / rel).read_text() + text)
        edited_build = base / f"core-build-{name}"
        _, moved[name] = identity_of(edited_build, target, *definitions,
                                     generator=generator, source=tree)
        if name.startswith("provider-source"):
            require(foreign_bytes_of(edited_build, name) == baseline_foreign,
                    f"{name}: a provider source edit changed the TPE flag or X bytes")

    # A provider flag change: edit the root's per-source recipe of the two providers. The Sobol
    # digest moves, both provider commands carry the option, and the TPE flag files and the
    # return-statistics outputs stay byte-identical (per-source options never entered either).
    def provider_flag(destination):
        with_sobol(destination)
        path = destination / "CMakeLists.txt"
        text = path.read_text()
        recipe = ('COMPILE_OPTIONS "-fno-fast-math;-ffp-contract=off;-frounding-math;'
                  '-fno-builtin;-fno-lto")')
        require(text.count(recipe) == 1, "the providers' per-source recipe is not in the root")
        path.write_text(text.replace(recipe, recipe[:-2] + ';-fno-trapping-math")'))

    flag_tree = base / "core-provider-flag"
    copy_tree(repository, flag_tree, provider_flag)
    flag_build = base / "core-build-provider-flag"
    flag_descriptor, moved["provider-flag"] = identity_of(flag_build, target, *definitions,
                                                         generator=generator, source=flag_tree)
    for unit in ("tpe_sampler.cpp", "search_space.cpp"):
        head = f"command provider <src>/src/core/{unit}\n"
        hit = [b for b in blocks_of(flag_descriptor) if b.startswith(head)]
        require(len(hit) == 1 and "arg -fno-trapping-math\n" in hit[0],
                f"the changed per-source option of {unit} is not in its bound command")
    require(foreign_bytes_of(flag_build, "provider-flag") == baseline_foreign,
            "a provider flag change moved the TPE flag bytes or the X outputs")
    require(len({digest, *moved.values()}) == len(moved) + 1, f"collision: {moved}")
    print(f"PASS [{generator}]: real combined target: actual C++ and C commands, header closure, "
          f"provider closure, executed commands, {len(moved)} perturbations (provider flag and "
          f"source edits included), TPE/X bytes unchanged "
          f"({'integrated' if integrated else 'snippet appended'} tree)")


def available_generators():
    found = []
    if shutil.which("make"):
        found.append("Unix Makefiles")
    if shutil.which("ninja"):
        found.append("Ninja")
    require(found, "neither make nor ninja is available")
    return found


GENERATOR_TOKEN = {"Unix Makefiles": "makefiles", "Ninja": "ninja"}


def scenario_plan(dlib, generators):
    """Every scenario of the default run as (token, step(base)), in the default order.

    The default run executes all of them. `--only` selects a subset by token so that the long
    real-project scenarios can be split into separately receipted proofs, and `--list` prints the
    tokens, so a proof can show that its parts are exactly the default run's scenarios. Each
    scenario writes only into its own subdirectories of `base`: a subset runs the same code on
    the same inputs as the corresponding part of the default run.
    """
    steps = [("text_hygiene", lambda base: scenario_text_hygiene(REPOSITORY)),
             ("providers_complete", lambda base: scenario_providers_complete(REPOSITORY)),
             ("synthetic", scenario_synthetic),
             ("fail_closed", scenario_fail_closed),
             ("same_rule_as_x", scenario_same_rule_as_x),
             ("ingredients", lambda base: ingredients_group(base, generators))]
    for generator in generators:
        label = GENERATOR_TOKEN[generator]
        steps.append((f"fixture:{label}", lambda base, g=generator: scenario_fixture(base, g)))
        steps.append((f"executed:{label}", lambda base, g=generator: scenario_executed(base, g)))
    steps.append(("not_circular", lambda base: scenario_not_circular()))
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
    with tempfile.TemporaryDirectory(prefix="pfh-sobol-") as temporary:
        base = Path(temporary)
        for token, step in plan:
            if only is None or token in only:
                print(f"SCENARIO {token}", flush=True)
                step(base)
                print(f"SCENARIO-DONE {token}", flush=True)
    if only is None:
        print("PASS: sobol numeric build identity")
    else:
        print("PASS-PARTIAL: sobol numeric build identity, selected scenarios: " + ",".join(only))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
