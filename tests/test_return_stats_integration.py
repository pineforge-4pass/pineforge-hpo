#!/usr/bin/env python3
"""Integration proofs for the return-statistics build identity and its fail-closed use.

Usage: test_return_stats_integration.py NATIVE PLUGIN BUILD_DIR SOURCE_DIR ENGINE_ROOT DLIB_SOURCE
       [unittest arguments]

UNEXECUTED until the proof phase. It is slow and serial: it rebuilds the real reducer object with
a verbose build and configures (and for the first case builds) fresh trees of the whole project.
With PFH_REQUIRE_RETURN_STATS_IDENTITY=1 (the proof configuration) an unbound main build is a
failure instead of a skip.

1. Bound facts of the main build: the retained identity is the v2 prefix plus the SHA-256 of the
   retained descriptor, and a requested run emits exactly that identity.
2. Actual-core effective command versus emitted descriptor. The compilation database is generator
   intent; the command the build really executes is captured from a verbose rebuild of the real
   reducer object, normalized by this file's own implementation of the documented rule, and
   compared token for token with the descriptor, and with the database entry. A mismatch between
   the executed command and the database is a failure, not a pass.
3. An explicit CMAKE_EXPORT_COMPILE_COMMANDS=OFF tree is unbound (compile_database_disabled): a
   requested run is refused with the registered diagnostic before the plugin is loaded, before any
   trial and before any output file is opened, while ordinary runs work and their bytes equal the
   bound build's.
4. A multi-configuration generator tree reports multi_config_generator (identity target only).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


NATIVE = Path(sys.argv.pop(1)).resolve()
PLUGIN = Path(sys.argv.pop(1)).resolve()
BUILD = Path(sys.argv.pop(1)).resolve()
SOURCE = Path(sys.argv.pop(1)).resolve()
ENGINE_ROOT = Path(sys.argv.pop(1)).resolve()
DLIB_SOURCE = Path(sys.argv.pop(1)).resolve()

CONTRACT = "pineforge-hpo-return-stats/v1"
FORMAT_LINE = "pineforge-hpo-return-stats-identity/v2"
PREFIX = "pineforge-hpo-return-stats-build/v2:sha256:"
REQUIRED = os.environ.get("PFH_REQUIRE_RETURN_STATS_IDENTITY") == "1"
RETURN_STATS_FLAGS = ("-fno-fast-math", "-ffp-contract=off", "-frounding-math", "-fno-builtin",
                      "-fno-lto")
SPACE = ("--int-dim", "Length", "10", "13", "1")
PATH_OPTIONS = {"-I", "-isystem", "-iquote", "-idirafter", "-iframework", "-F", "-isysroot", "-B"}
DROPPED = {"-c", "-MD", "-MMD", "-MP", "-MG"}
DROPPED_WITH_ARGUMENT = {"-o", "-MF", "-MT", "-MQ"}


def identity_directory(build: Path) -> Path:
    root = build / "generated" / "return_stats_identity"
    directories = [path for path in root.iterdir() if path.is_dir()]
    if len(directories) != 1:
        raise AssertionError(f"expected one configuration directory in {root}: {directories}")
    return directories[0]


def read_identity(build: Path):
    directory = identity_directory(build)
    descriptor = (directory / "return_stats_identity.descriptor.txt").read_bytes()
    identity = (directory / "return_stats_identity.txt").read_text().rstrip("\n")
    header = (directory / "return_stats_identity_generated.hpp").read_text()
    return descriptor, identity, header


def rewrite(path: str, base: Path) -> str:
    """The documented, location-only rewrite of a path-valued token."""
    value = Path(os.path.normpath(path if os.path.isabs(path) else str(base / path)))
    for label, root in (("build", BUILD_FOR_REWRITE[0]), ("src", SOURCE)):
        try:
            relative = value.relative_to(root)
        except ValueError:
            continue
        return f"<{label}>" if str(relative) == "." else f"<{label}>/{relative}"
    return str(value)


BUILD_FOR_REWRITE = [BUILD]


def normalize(tokens: list[str], source: Path, base: Path, build: Path) -> list[str]:
    """This file's own implementation of the documented normalization rule."""
    BUILD_FOR_REWRITE[0] = build
    out: list[str] = []
    pending = ""
    for token in tokens[1:]:  # the first token is the compiler: bound by its bytes instead
        if pending == "drop":
            pending = ""
            continue
        if pending == "path":
            out.append(rewrite(token, base))
            pending = ""
            continue
        if pending == "include":
            absolute = token if os.path.isabs(token) else str(base / token)
            digest = hashlib.sha256(Path(absolute).read_bytes()).hexdigest()
            out.append(f"{rewrite(token, base)}#sha256={digest}")
            pending = ""
            continue
        if token.startswith("@"):
            raise AssertionError("response files are unbound by design")
        if token in DROPPED:
            continue
        if token in DROPPED_WITH_ARGUMENT:
            pending = "drop"
            continue
        if token in ("-include", "-imacros"):
            out.append(token)
            pending = "include"
            continue
        if token in PATH_OPTIONS:
            out.append(token)
            pending = "path"
            continue
        for joined in ("-I", "-F", "-B", "--sysroot="):
            if token.startswith(joined) and len(token) > len(joined):
                out.append(joined + rewrite(token[len(joined):], base))
                break
        else:
            out.append(rewrite(token, base) if token == str(source) else token)
    if pending:
        raise AssertionError("the command ends inside an option argument")
    return out


def descriptor_arguments(descriptor: str, source_display: str) -> list[str]:
    lines = descriptor.split("\n")
    start = lines.index(f"command {source_display}")
    arguments = []
    for line in lines[start + 1:]:
        if not line.startswith("arg "):
            break
        arguments.append(line[4:])
    return arguments


def run(command, **kwargs):
    return subprocess.run(command, text=True, capture_output=True, check=False, **kwargs)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="pf_rs_int_")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.csv = self.directory / "bars.csv"
        self.csv.write_text(
            "timestamp,open,high,low,close,volume\n"
            + "".join(f"{1700000000000 + index * 60000},100,102,99,101,10\n"
                      for index in range(64)),
            encoding="utf-8")

    def argv(self, native, *extra, plugin=None, sampler="grid"):
        return [str(native), "run", "--strategy", str(plugin or PLUGIN), "--ohlcv", str(self.csv),
                "--objective", "metrics.all.net_profit", "--input-tf", "1", "--script-tf", "5",
                "--chart-timezone", "UTC", "--bar-magnifier", "true", "--magnifier-samples", "6",
                "--magnifier-distribution", "triangle", "--fixed-input", "BatchPrefixTest", "1",
                *SPACE, "--sampler", sampler, *map(str, extra)]


class MainBuildTests(Base):
    def bound_or_skip(self):
        descriptor, identity, header = read_identity(BUILD)
        if b"capability=unbound" in descriptor:
            reason = descriptor.decode().split("reason=")[1].split("\n")[0]
            if REQUIRED:
                self.fail(f"the main build's identity is unbound ({reason}) but is required")
            self.skipTest(f"the main build is unbound ({reason}); the bound facts are not checked")
        return descriptor, identity, header

    def test_bound_facts_and_the_emitted_identity(self) -> None:
        descriptor, identity, header = self.bound_or_skip()
        text = descriptor.decode()
        self.assertEqual(text.split("\n")[0], FORMAT_LINE)
        self.assertIn(f"contract={CONTRACT}\n", text)
        self.assertEqual(identity, PREFIX + hashlib.sha256(descriptor).hexdigest())
        self.assertIn("inline constexpr bool kBound = true;", header)
        for flag in RETURN_STATS_FLAGS:
            self.assertIn(f"\narg {flag}\n", text)
        completed = run(self.argv(NATIVE, "--max-trials", "2", "--record-metric",
                                  "returns.bar.count"))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        stats = json.loads(completed.stdout)["return_stats"]
        self.assertEqual(stats["numeric_build_identity"], identity)
        self.assertEqual(stats["contract"], CONTRACT)
        self.assertEqual(stats["risk_free_annual"], 0.02)

    def test_the_executed_reducer_command_equals_the_descriptor(self) -> None:
        descriptor, _, _ = self.bound_or_skip()
        source = SOURCE / "src" / "core" / "return_stats.cpp"
        objects = list(BUILD.rglob("return_stats.cpp.o"))
        self.assertEqual(len(objects), 1, f"expected one reducer object: {objects}")
        objects[0].unlink()  # force the real invocation to run again
        build = run(["cmake", "--build", str(BUILD), "--target", "pineforge_hpo_core", "--verbose"],
                    timeout=1800)
        self.assertEqual(build.returncode, 0, build.stderr)
        executed = None
        for line in build.stdout.splitlines():
            if str(source) not in line or " -c " not in line:
                continue
            tokens = shlex.split(line)
            if "&&" in tokens:  # Makefile generator: "cd <dir> && <compiler> ..."
                tokens = tokens[len(tokens) - tokens[::-1].index("&&"):]
            executed = tokens
        self.assertIsNotNone(executed, "the verbose build never printed the reducer command")
        source_display = "<src>/src/core/return_stats.cpp"
        from_build = normalize(executed, source, BUILD, BUILD)
        emitted = descriptor_arguments(descriptor.decode(), source_display)
        self.assertEqual(from_build, emitted,
                         "the executed command differs from the emitted descriptor")
        # The database is generator intent: it must agree with what was executed.
        database = json.loads((BUILD / "compile_commands.json").read_text())
        entries = [entry for entry in database
                   if Path(entry["file"]).resolve() == source and "/pineforge_hpo_core.dir/" in
                   entry.get("command", "")]
        self.assertEqual(len(entries), 1)
        from_database = normalize(shlex.split(entries[0]["command"]), source,
                                  Path(entries[0]["directory"]), BUILD)
        self.assertEqual(from_database, from_build,
                         "compile_commands.json differs from the executed invocation")
        # Compiler custody: the executed driver's bytes are the bound ones.
        driver = Path(executed[0])
        driver = Path(shutil.which(str(driver)) or driver).resolve()
        digest = hashlib.sha256(driver.read_bytes()).hexdigest()
        self.assertIn(f"\ncompiler.sha256={digest}\n", descriptor.decode())
        # Regenerating the identity after the rebuild must give the same descriptor.
        regenerated = run(["cmake", "--build", str(BUILD), "--target",
                           "pineforge_hpo_core_return_stats_identity"], timeout=600)
        self.assertEqual(regenerated.returncode, 0, regenerated.stderr)
        self.assertEqual(read_identity(BUILD)[0], descriptor)


class UnboundTreeTests(Base):
    def configure(self, tree: Path, *options: str):
        return run(["cmake", "-S", str(SOURCE), "-B", str(tree), "-DCMAKE_BUILD_TYPE=Release",
                    "-DPINEFORGE_HPO_BUILD_TESTS=OFF", f"-DPINEFORGE_ENGINE_ROOT={ENGINE_ROOT}",
                    f"-DFETCHCONTENT_SOURCE_DIR_DLIB={DLIB_SOURCE}", *options], timeout=900)

    def test_explicit_off_tree_refuses_requests_and_keeps_ordinary_runs(self) -> None:
        tree = self.directory / "unbound"
        configured = self.configure(tree, "-DCMAKE_EXPORT_COMPILE_COMMANDS=OFF")
        self.assertEqual(configured.returncode, 0, configured.stderr)
        built = run(["cmake", "--build", str(tree), "--target", "pineforge-hpo-native", "-j",
                     str(os.cpu_count() or 2)], timeout=3000)
        self.assertEqual(built.returncode, 0, built.stderr[-4000:])
        descriptor, identity, header = read_identity(tree)
        self.assertEqual(descriptor.decode(),
                         f"{FORMAT_LINE}\ncontract={CONTRACT}\ncapability=unbound\n"
                         "reason=compile_database_disabled\n")
        self.assertEqual(identity, "")
        self.assertIn("inline constexpr bool kBound = false;", header)
        unbound = tree / "bin" / "pineforge-hpo-native"
        self.assertTrue(unbound.is_file())

        # A requested run is refused before the plugin, the dataset or any output file.
        never = self.directory / "never-created.ndjson"
        for plugin in (PLUGIN, self.directory / "no-such-plugin"):
            for metric in ("returns.bar.count", "returns.monthly.status"):
                with self.subTest(plugin=plugin.name, metric=metric):
                    refused = run(self.argv(unbound, "--max-trials", "2", "--record-metric",
                                            metric, "--trials-file", never, plugin=plugin))
                    self.assertEqual(refused.returncode, 1, refused.stderr)
                    failure = json.loads(refused.stdout)["failure"]
                    self.assertEqual(failure["code"], "hpo_toolchain_unavailable")
                    self.assertEqual(failure["args"], {"reason": "native_runner"})
                    self.assertIn("compile_database_disabled", refused.stderr)
                    self.assertNotIn("trials", json.loads(refused.stdout))
                    self.assertFalse(never.exists(), "an output file was opened before the refusal")
        objective = self.argv(unbound, "--max-trials", "2")
        objective[objective.index("--objective") + 1] = "returns.bar.mean"
        self.assertEqual(json.loads(run(objective).stdout)["failure"]["code"],
                         "hpo_toolchain_unavailable")

        # Ordinary runs are untouched, and their bytes equal the bound build's.
        for sampler, extra in (("grid", ("--max-trials", "4")),
                               ("random", ("--max-trials", "6", "--seed", "3"))):
            with self.subTest(ordinary=sampler):
                theirs = run(self.argv(unbound, *extra, sampler=sampler))
                ours = run(self.argv(NATIVE, *extra, sampler=sampler))
                self.assertEqual(theirs.returncode, 0, theirs.stderr)
                self.assertEqual(theirs.returncode, ours.returncode)
                self.assertEqual(theirs.stdout, ours.stdout)
                self.assertNotIn("return_stats", theirs.stdout)
        self.assertEqual(run([str(unbound), "--help"]).stdout, run([str(NATIVE), "--help"]).stdout)

    def test_a_multi_configuration_generator_reports_it(self) -> None:
        if shutil.which("ninja") is None:
            self.skipTest("ninja is not installed: the multi-configuration case was not checked")
        tree = self.directory / "multi"
        configured = self.configure(tree, "-G", "Ninja Multi-Config",
                                    "-DPINEFORGE_HPO_BUILD_NATIVE_CLI=OFF",
                                    "-DPINEFORGE_HPO_BUILD_ENGINE_ADAPTER=OFF")
        self.assertEqual(configured.returncode, 0, configured.stderr)
        built = run(["cmake", "--build", str(tree), "--config", "Release", "--target",
                     "pineforge_hpo_core_return_stats_identity"], timeout=600)
        self.assertEqual(built.returncode, 0, built.stderr)
        descriptor, identity, header = read_identity(tree)
        self.assertIn("reason=multi_config_generator\n", descriptor.decode())
        self.assertEqual(identity, "")
        self.assertIn("inline constexpr bool kBound = false;", header)


if __name__ == "__main__":
    unittest.main()
