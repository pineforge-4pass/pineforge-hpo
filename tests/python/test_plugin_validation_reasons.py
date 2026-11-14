"""Branch-specific ``hpo_plugin_invalid`` reasons of the Python plugin validator.

Every row calls the real ``ArtifactBuilder`` validator methods. The native rows load
tiny shared libraries that ``setUpClass`` compiles from generated C source with the
system C compiler, so a load, symbol or ABI failure comes from the real loader and
never from a mocked ``ctypes`` call. A missing compiler, or an environment that
cannot load a freshly built library, is a setup ERROR: it is neither a pass nor an
assertion failure, and it proves nothing about the validator.

The supplied-validator rows use ``Mock`` objects only as external inputs: the
exception or result that a caller's validator would raise or return. The code under
test, ``ArtifactBuilder._run_plugin_validator``, is always the real method.

Only the default validator's own steps report ``load``, ``symbols`` and ``abi``.
What a supplied validator raises untyped, or returns without confirming the engine
ABI, stays ``validation``.
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "external" / "pineforge-codegen-oss"))

from pineforge_hpo.artifact import ArtifactBuildError, ArtifactBuilder  # noqa: E402
from pineforge_hpo.error import HpoError, failure_document  # noqa: E402
from pineforge_hpo.window_plan import WindowPlanError  # noqa: E402

CATALOG_PATH = ROOT / "python" / "pineforge_hpo" / "hpo_failure_codes.json"
CATALOG = json.loads(CATALOG_PATH.read_bytes())
REASON_VALUES = CATALOG["codes"]["hpo_plugin_invalid"]["args"]["reason"]["values"]
REQUIRED_SYMBOLS = [
    "pf_abi_version",
    "strategy_create",
    "strategy_free",
    "strategy_set_input",
    "strategy_set_override",
    "strategy_get_last_error",
    "run_backtest_full",
    "report_free",
]
# Diagnostics the default validator itself produces for its load, symbols and ABI
# steps. The native rows assert that the real validator emits exactly these; the
# untyped-exception row feeds the same text to a supplied validator.
LOAD_PREFIX = "cannot load compiled strategy plugin: "
SYMBOLS_PREFIX = "compiled strategy plugin is missing required symbols: "
# The public failure document documented in docs/failure-codes.md for a load failure.
LOAD_FAILURE_DOCUMENT = {
    "schema_version": 1,
    "ok": False,
    "failure": {
        "origin": "hpo",
        "code": "hpo_plugin_invalid",
        "args": {"reason": "load"},
        "exit_code": 1,
    },
}


def abi_mismatch_message(actual: int, expected: int) -> str:
    return f"strategy plugin ABI {actual} does not match engine ABI {expected}"


def find_c_compiler() -> str:
    for candidate in (os.environ.get("CC"), "cc", "gcc", "clang"):
        found = shutil.which(candidate) if candidate else None
        if found:
            return found
    raise RuntimeError(
        "no C compiler (CC, cc, gcc or clang) is available to build native plugin "
        "fixtures: a setup failure, not a validator result"
    )


def plugin_source(abi: int, omit: tuple[str, ...] = ()) -> str:
    lines = []
    for symbol in REQUIRED_SYMBOLS:
        if symbol in omit:
            continue
        if symbol == "pf_abi_version":
            lines.append(f"int pf_abi_version(void) {{ return {abi}; }}")
        else:
            lines.append(f"int {symbol}(void) {{ return 0; }}")
    return "\n".join(lines) + "\n"


def build_plugin(
    directory: Path,
    compiler: str,
    name: str,
    *,
    abi: int,
    omit: tuple[str, ...] = (),
) -> Path:
    source = directory / f"{name}.c"
    library = directory / f"{name}.so"
    source.write_text(plugin_source(abi, omit), encoding="utf-8")
    command = [compiler, "-shared", "-fPIC", "-O0", "-o", str(library), str(source)]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0 or not library.is_file():
        raise RuntimeError(
            f"cannot build native plugin fixture {name}: "
            f"exit {completed.returncode}: {completed.stderr}"
        )
    return library


class ReasonAssertions:
    def assert_plugin_invalid(
        self, error: BaseException, reason: str, message: str
    ) -> None:
        self.assertIsInstance(error, ArtifactBuildError)
        self.assertEqual(error.stage, "plugin_validation")
        self.assertEqual(error.code, "hpo_plugin_invalid")
        self.assertEqual(error.origin, "hpo")
        self.assertEqual(error.failure_args, {"reason": reason})
        self.assertIn(reason, REASON_VALUES)
        self.assertEqual(str(error), message)
        self.assertEqual(error.args, (message,))

    def refusal(self, call, *arguments) -> ArtifactBuildError:
        with self.assertRaises(ArtifactBuildError) as caught:
            call(*arguments)
        return caught.exception


class NativePluginValidationReasonTests(ReasonAssertions, unittest.TestCase):
    ENGINE_ABI = 2

    @classmethod
    def setUpClass(cls) -> None:
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.directory = Path(temporary.name).resolve()
        compiler = find_c_compiler()
        cls.complete = {
            abi: build_plugin(cls.directory, compiler, f"complete-abi{abi}", abi=abi)
            for abi in (0, 2, 5)
        }
        cls.lacking = {
            symbol: build_plugin(
                cls.directory, compiler, f"lacking-{symbol}", abi=2, omit=(symbol,)
            )
            for symbol in REQUIRED_SYMBOLS
        }
        cls.lacking_two = build_plugin(
            cls.directory,
            compiler,
            "lacking-two",
            abi=2,
            omit=("run_backtest_full", "report_free"),
        )
        # Lacks one export and also reports an ABI other than the engine's, so the
        # order of the symbol step and the ABI step decides which reason is raised.
        cls.lacking_and_mismatched = build_plugin(
            cls.directory,
            compiler,
            "lacking-and-mismatched",
            abi=5,
            omit=("report_free",),
        )
        # Setup guard: the environment itself must load a freshly built library.
        # A refusal here is a setup ERROR, never a validator result.
        ctypes.CDLL(str(cls.complete[2]))

    def builder(self) -> ArtifactBuilder:
        return ArtifactBuilder(
            engine_root=self.directory / "engine-not-needed",
            cache_dir=self.directory / "cache-not-needed",
        )

    def entries(self):
        # No plugin_validator is supplied, so the default validator is the real
        # _validate_plugin, reached both directly and through the real seam.
        builder = self.builder()
        return (
            ("_validate_plugin", builder._validate_plugin),
            ("_run_plugin_validator", builder._run_plugin_validator),
        )

    def test_complete_plugin_passes_with_real_abi_and_symbols(self) -> None:
        # Proves the fixture plugins load and are complete, so every failing row
        # fails only for its own fault, and that the result carries the ABI the
        # native library reports (including a falsy 0) plus the full required-symbol
        # list. This is the allowed-plugin control for every refusal row below.
        # False pass: a constant validator result, or a fixture that is itself
        # unloadable, would let the failing rows hold for the wrong reason.
        for abi in (0, 2, 5):
            for name, entry in self.entries():
                with self.subTest(abi=abi, entry=name):
                    self.assertEqual(
                        dict(entry(self.complete[abi], abi)),
                        {"abi_version": abi, "required_symbols": REQUIRED_SYMBOLS},
                    )

    def test_unloadable_plugin_reports_load_reason(self) -> None:
        # Proves a real loader refusal is the "load" branch whatever makes the path
        # unloadable (absent, empty, not a shared object, a directory); the OSError
        # that ctypes raised stays the cause and the diagnostic is built from it. The
        # public failure document carries reason "load", as documented.
        # False pass: a constant reason, a reason parsed from diagnostic text, or an
        # absent file reported as a different reason.
        garbage = self.directory / "garbage.so"
        garbage.write_bytes(b"this is text, not a shared object\n")
        empty = self.directory / "empty.so"
        empty.write_bytes(b"")
        files = {
            "missing": self.directory / "missing.so",
            "garbage": garbage,
            "empty": empty,
            "directory": self.directory,
        }
        for label, path in files.items():
            for name, entry in self.entries():
                with self.subTest(file=label, entry=name):
                    error = self.refusal(entry, path, self.ENGINE_ABI)
                    cause = error.__cause__
                    self.assertIsInstance(cause, OSError)
                    message = f"{LOAD_PREFIX}{cause}"
                    self.assert_plugin_invalid(error, "load", message)
                    document = failure_document(error, 1)
                    self.assertEqual(document, LOAD_FAILURE_DOCUMENT)

    def test_missing_required_symbol_reports_symbols_reason(self) -> None:
        # Proves each required export is probed: a plugin lacking exactly one of the
        # eight, or two at once, is refused as "symbols" and the diagnostic names
        # the absent exports in required order.
        # False pass: one reason for every branch, or a check keyed to a single
        # symbol or to a fixed message.
        prefix = SYMBOLS_PREFIX
        for symbol in REQUIRED_SYMBOLS:
            for name, entry in self.entries():
                with self.subTest(symbol=symbol, entry=name):
                    path = self.lacking[symbol]
                    error = self.refusal(entry, path, self.ENGINE_ABI)
                    self.assert_plugin_invalid(error, "symbols", prefix + symbol)
                    self.assertIsNone(error.__cause__)
        two = "run_backtest_full, report_free"
        for name, entry in self.entries():
            with self.subTest(symbols="two", entry=name):
                error = self.refusal(entry, self.lacking_two, self.ENGINE_ABI)
                self.assert_plugin_invalid(error, "symbols", prefix + two)
        # The symbol step runs before the ABI step: a plugin that lacks an export
        # and also reports another ABI is refused as "symbols", not as "abi". A
        # wrong ABI alone is refused as "abi" in the next test.
        for name, entry in self.entries():
            with self.subTest(symbols="missing and wrong ABI", entry=name):
                error = self.refusal(entry, self.lacking_and_mismatched, 2)
                self.assert_plugin_invalid(error, "symbols", prefix + "report_free")

    def test_abi_mismatch_reports_abi_reason(self) -> None:
        # Proves the ABI the loaded library reports is compared with the expected
        # engine ABI and that both numbers reach the diagnostic. The same plugin
        # passes for its own ABI (the control in the first test) and is refused for
        # any other one, including 0 on either side.
        # False pass: a constant reason or message, or a comparison that ignores
        # the library's ABI or the expected one.
        pairs = ((2, 1), (2, 3), (2, 5), (5, 2), (5, 4), (5, 6), (0, 2), (2, 0))
        for lib, want in pairs:
            message = abi_mismatch_message(lib, want)
            for name, entry in self.entries():
                with self.subTest(library_abi=lib, expected=want, entry=name):
                    error = self.refusal(entry, self.complete[lib], want)
                    self.assert_plugin_invalid(error, "abi", message)
                    self.assertIsNone(error.__cause__)


class SuppliedValidatorSeamTests(ReasonAssertions, unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        # A supplied validator never opens this path; only its identity is checked.
        self.plugin = self.directory / "strategy.so"

    def run_validator(self, supplied, expected_abi: int = 2):
        builder = ArtifactBuilder(
            engine_root=self.directory / "engine-not-needed",
            cache_dir=self.directory / "cache-not-needed",
            plugin_validator=supplied,
        )
        return builder._run_plugin_validator(self.plugin, expected_abi)

    def test_typed_artifact_build_error_is_retained(self) -> None:
        # Proves the existing seam: a typed ArtifactBuildError raised by a supplied
        # validator is the exception that propagates, with its metadata untouched,
        # including the catalog reasons the default validator never emits itself.
        # False pass: re-raising a copy, rebuilding the code and arguments from its
        # stage or its text, or replacing a typed reason by the generic one.
        for reason in ("path", "platform", "setter_missing"):
            with self.subTest(reason=reason):
                message = f"typed build error: {reason}"
                original = ArtifactBuildError("plugin_validation", message)
                original.with_failure("hpo_plugin_invalid", {"reason": reason})
                supplied = Mock(side_effect=original)
                with self.assertRaises(ArtifactBuildError) as caught:
                    self.run_validator(supplied)
                self.assertIs(caught.exception, original)
                self.assert_plugin_invalid(caught.exception, reason, message)

    def test_other_typed_errors_are_not_rewrapped(self) -> None:
        # Proves any typed HpoError, not only an ArtifactBuildError of the plugin
        # stage, passes through the seam as the same object with the code, origin
        # and arguments it was raised with, including a typed reason, a null
        # argument object and a stage other than plugin_validation.
        # False pass: wrapping it in a generic "validation" ArtifactBuildError, or
        # rebuilding its metadata from its type, stage or text.
        plugin_error = HpoError("typed plugin error")
        plugin_error.with_failure("hpo_plugin_invalid", {"reason": "path"})
        engine_error = HpoError("typed engine error")
        engine_error.with_failure("future_engine_failure", None, "engine")
        planner_error = WindowPlanError("window_plan_path", "typed planner error")
        stage_error = ArtifactBuildError("configuration", "typed configuration error")
        for original in (plugin_error, engine_error, planner_error, stage_error):
            expected = (original.code, original.origin, original.failure_args)
            with self.subTest(code=original.code):
                supplied = Mock(side_effect=original)
                with self.assertRaises(HpoError) as caught:
                    self.run_validator(supplied)
                failure = caught.exception
                self.assertIs(failure, original)
                self.assertIsNone(failure.__cause__)
                actual = (failure.code, failure.origin, failure.failure_args)
                self.assertEqual(actual, expected)
                supplied.assert_called_once_with(self.plugin, 2)

    def test_untyped_exceptions_stay_validation(self) -> None:
        # Proves an exception without typed metadata is classified only as
        # "validation", whatever its type or text says, and stays the cause. The
        # texts below resemble load, platform, ABI and symbol diagnostics on purpose.
        # The production texts are exactly what the default validator itself
        # produces for its load, symbols and ABI steps (the native rows pin them).
        # A supplied validator that raises that text as a plain exception, of any of
        # several types, has taken no such step, so it is still only "validation".
        # A result that dict() rejects is refused the same way, with the conversion
        # error as the cause.
        # False pass: parsing diagnostic text, or the exception type, into the
        # load, path, platform, abi or symbols reasons.
        originals = [
            RuntimeError("validator exploded"),
            ValueError("bad validator input"),
            OSError("cannot open shared object file: No such file or directory"),
            OSError("operation not supported on this platform"),
            OSError("plugin ABI 3 does not match engine ABI 2"),
            AttributeError("undefined symbol: report_free"),
        ]
        production_texts = (
            LOAD_PREFIX + "/plugins/strategy.so: cannot open shared object file",
            SYMBOLS_PREFIX + "run_backtest_full, report_free",
            abi_mismatch_message(3, 2),
        )
        for text in production_texts:
            for kind in (RuntimeError, ValueError, OSError, AttributeError):
                originals.append(kind(text))
        for original in originals:
            with self.subTest(original=repr(original)):
                supplied = Mock(side_effect=original)
                error = self.refusal(self.run_validator, supplied)
                self.assertIs(error.__cause__, original)
                message = f"strategy plugin validation failed: {original}"
                self.assert_plugin_invalid(error, "validation", message)
        with self.subTest(returned="None, which dict() rejects"):
            error = self.refusal(self.run_validator, Mock(return_value=None))
            self.assertIsInstance(error.__cause__, TypeError)
            message = f"strategy plugin validation failed: {error.__cause__}"
            self.assert_plugin_invalid(error, "validation", message)

    def test_unconfirmed_abi_result_keeps_validation_reason(self) -> None:
        # Proves a result that a supplied validator returns without confirming the
        # expected engine ABI keeps the stage default "validation", whether the
        # returned ABI differs from the expected one (several pairs) or is absent
        # ({} or None). The builder observed no ABI step here, only what the callback
        # chose to return, so it infers no "abi" mismatch from it, least of all from
        # missing data; only the default validator's own ABI step does (native rows).
        # A confirming result is returned unchanged.
        # False pass: giving this branch a reason of its own, "abi" in particular,
        # or a check that ignores the expected ABI.
        message = "strategy plugin validator did not confirm the expected engine ABI"
        for want, got in ((2, 1), (2, 3), (7, 2), (7, 8), (3, 30)):
            with self.subTest(want=want, got=got):
                result = {"abi_version": got, "required_symbols": REQUIRED_SYMBOLS}
                supplied = Mock(return_value=result)
                error = self.refusal(self.run_validator, supplied, want)
                self.assert_plugin_invalid(error, "validation", message)
                self.assertIsNone(error.__cause__)
        for label, result in (("empty", {}), ("none", {"abi_version": None})):
            with self.subTest(result=label):
                supplied = Mock(return_value=result)
                error = self.refusal(self.run_validator, supplied)
                self.assert_plugin_invalid(error, "validation", message)
                self.assertIsNone(error.__cause__)
        for want in (2, 7):
            with self.subTest(confirmed=want):
                result = {"abi_version": want, "required_symbols": REQUIRED_SYMBOLS}
                returned = self.run_validator(Mock(return_value=result), want)
                self.assertEqual(returned, result)


if __name__ == "__main__":
    unittest.main()
