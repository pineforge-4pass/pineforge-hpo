"""One FD6 source/catalog/real-projector conformance target; stdlib only."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import string
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PREFIX = "hpo_fd6_"
UINT64_MAX = (1 << 64) - 1
IDS = (0, 17, (1 << 63) + 17, UINT64_MAX)
LEGACY_CATALOG_SHA256 = (
    "db143f09b7c4ee11b755fc943d9ebaf86f4cc217c85c5781f6fa8d48c6bcfc72"
)
ID_MESSAGES = {
    "hpo_fd6_finalized": ("FD6 projector is finalized",),
    "hpo_fd6_below_floor": ("FD6 trial ID is below the projector floor",),
    "hpo_fd6_duplicate_issue": ("FD6 trial ID was issued more than once",),
    "hpo_fd6_unknown_status": ("FD6 producer status is unknown",),
    "hpo_fd6_unissued_trial": (
        "FD6 producer record names an unissued trial ID",
        "FD6 skipped record names an unissued trial ID",
    ),
    "hpo_fd6_pending_after_terminal": (
        "FD6 pending record arrived after a terminal record",
    ),
    "hpo_fd6_pending_after_skip": ("FD6 pending record arrived after a skipped trial",),
    "hpo_fd6_duplicate_terminal": ("FD6 trial ID received a second terminal record",),
    "hpo_fd6_skip_after_terminal": ("FD6 trial was skipped after a terminal record",),
    "hpo_fd6_duplicate_skip": ("FD6 trial ID was skipped more than once",),
}
NO_ARG_MESSAGES = {
    "hpo_fd6_flush_finalized": ("FD6 projector is finalized",),
    "hpo_fd6_internal": (
        "FD6 terminal row was already pending",
        "FD6 terminal ID was already recorded",
    ),
    "hpo_fd6_counter_overflow": ("FD6 counter arithmetic would overflow",),
}
MESSAGES = ID_MESSAGES | NO_ARG_MESSAGES
STATIC_ONLY = frozenset({"hpo_fd6_internal", "hpo_fd6_counter_overflow"})
CASES = {
    "finalized_issue": ("hpo_fd6_finalized", 0),
    "finalized_observe": ("hpo_fd6_finalized", 0),
    "finalized_skip": ("hpo_fd6_finalized", 0),
    "below_floor": ("hpo_fd6_below_floor", 0),
    "duplicate_issue": ("hpo_fd6_duplicate_issue", 0),
    "unknown_status": ("hpo_fd6_unknown_status", 0),
    "unissued_observe": ("hpo_fd6_unissued_trial", 0),
    "unissued_skip": ("hpo_fd6_unissued_trial", 1),
    "pending_after_terminal": ("hpo_fd6_pending_after_terminal", 0),
    "pending_after_skip": ("hpo_fd6_pending_after_skip", 0),
    "duplicate_terminal": ("hpo_fd6_duplicate_terminal", 0),
    "skip_after_terminal": ("hpo_fd6_skip_after_terminal", 0),
    "duplicate_skip": ("hpo_fd6_duplicate_skip", 0),
    "finalized_flush": ("hpo_fd6_flush_finalized", 0),
}

# This is a literal-site inventory of the current C++ idioms, not a C++ parser.
# Reject every FD6 literal not accounted for by a recognized real throw/refusal.
CPP_TEXT = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"')
CODE_LITERAL = r'"(?P<code>hpo_fd6_[a-z0-9_]+)"'
MESSAGE_LITERAL = r'(?P<message>"(?:\\.|[^"\\])*")'
REFUSAL = re.compile(
    r"\brefuse\s*\(\s*"
    + CODE_LITERAL
    + r"\s*,\s*(?:trial_id|trial\s*\.\s*trial_id)\s*,\s*"
    + MESSAGE_LITERAL
    + r"\s*(?:,\s*(?:true|false)\s*)?\)"
)
DIRECT_THROW = re.compile(
    r"\bthrow\s+TypedHpoError\s*<\s*std\s*::\s*"
    r"(?P<family>invalid_argument|logic_error|overflow_error)\s*>\s*\(\s*"
    + CODE_LITERAL
    + r"\s*,\s*\{\s*\}\s*,\s*"
    + MESSAGE_LITERAL
    + r"\s*\)"
)
REFUSE_DEFINITION = re.compile(
    r"\bvoid\s+refuse\s*\((?P<parameters>[^()]*)\)\s*"
    r"\{(?P<body>[^{}]*)\}"
)


class InputFailure(ValueError):
    """Missing or malformed input is never a skip."""


class ChildFailure(InputFailure):
    def __init__(self, returncode: int):
        super().__init__(f"native child failed, raw returncode={returncode}")
        self.returncode = returncode


@dataclass
class SourceSite:
    code: str
    message: str
    args: dict[str, str]
    family: str
    location: str

    def signature(self):
        return self.code, self.message, tuple(sorted(self.args.items())), self.family


def need(condition: bool, message: str) -> None:
    if not condition:
        raise InputFailure(message)


def compare(actual, expected, label: str, failures: list[str]) -> None:
    if type(actual) is not type(expected) or actual != expected:
        failures.append(f"{label}: got {actual!r}, expected {expected!r}")


def compare_members(actual: set, expected: set, label: str, failures: list[str]) -> None:
    if actual != expected:
        failures.append(
            f"{label}: missing={sorted(expected - actual, key=repr)!r}, "
            f"unexpected={sorted(actual - expected, key=repr)!r}"
        )


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        need(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_constant(value: str):
    raise InputFailure(f"non-JSON numeric constant: {value}")


def parse_json(payload: bytes):
    need(bool(payload.strip()), "empty JSON input / no observations")
    return json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )


def required_file(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    need(resolved.is_file(), f"not a regular file: {path}")
    return resolved


def canonical_validator():
    path = required_file(ROOT / "scripts/gen_catalog_diff.py")
    # Import only; never call the generator main, Git helpers, or write bytecode.
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("fd6_canonical_catalog", path)
    need(spec is not None and spec.loader is not None, f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_catalog


def source_sites() -> list[SourceSite]:
    sites = []
    for directory in (ROOT / "src", ROOT / "include"):
        need(directory.is_dir(), f"missing source directory: {directory}")
        files = sorted(
            path
            for path in directory.rglob("*")
            if path.is_file()
            and path.suffix in {".cpp", ".hpp", ".h", ".cc", ".cxx", ".hxx", ".c"}
        )
        need(bool(files), f"empty source directory: {directory}")
        for path in files:
            text = path.read_text(encoding="utf-8")
            literals = [
                match
                for match in CPP_TEXT.finditer(text)
                if match.group().startswith('"hpo_fd6_')
            ]
            if not literals:
                continue
            clean = CPP_TEXT.sub(
                lambda match: (
                    match.group()
                    if match.group().startswith('"')
                    else re.sub(r"[^\n]", " ", match.group())
                ),
                text,
            )
            refusals = list(REFUSAL.finditer(clean))
            if refusals:
                definition = REFUSE_DEFINITION.search(clean)
                need(definition is not None, f"{path}: cannot resolve FD6 refuse metadata")
                parameters = re.sub(r"\s+", "", definition["parameters"])
                need(
                    parameters
                    == (
                        "constchar*code,std::uint64_ttrial_id,"
                        "constchar*message,boolrejected_slot=false"
                    ),
                    f"{path}: FD6 refuse no longer declares an unsigned trial_id",
                )
                body = re.sub(r"\s+", "", definition["body"])
                need(
                    'ErrorArgumentsargs;args.emplace("trial_id",ErrorArgument(trial_id));'
                    in body
                    and (
                        "throwTypedHpoError<std::invalid_argument>"
                        "(code,std::move(args),message);"
                    ) in body,
                    f"{path}: cannot establish FD6 refuse argument/family forwarding",
                )
            matches = [
                (match, {"trial_id": "uint64"}, "std::invalid_argument")
                for match in refusals
            ]
            matches.extend(
                (match, {}, "std::" + match["family"])
                for match in DIRECT_THROW.finditer(clean)
            )
            for literal in literals:
                owners = [
                    item
                    for item in matches
                    if item[0].start() <= literal.start() < item[0].end()
                ]
                line = text.count("\n", 0, literal.start()) + 1
                location = f"{path.relative_to(ROOT)}:{line}"
                need(len(owners) == 1, f"{location}: unaccounted FD6 source literal")
                match, args, family = owners[0]
                code = json.loads(literal.group())
                need(code == match["code"], f"{location}: source literal mismatch")
                sites.append(
                    SourceSite(code, json.loads(match["message"]), args, family, location)
                )
    need(bool(sites), "no FD6 source sites")
    return sites


def source_contract(sites: list[SourceSite], failures: list[str]) -> None:
    codes = {site.code for site in sites}
    compare_members(codes, set(MESSAGES), "source FD6 code set", failures)
    for site in sites:
        if site.code not in MESSAGES:
            failures.append(f"{site.location}: unexpected source code {site.code}")
            continue
        args = {"trial_id": "uint64"} if site.code in ID_MESSAGES else {}
        family = (
            "std::invalid_argument"
            if site.code in ID_MESSAGES
            else "std::overflow_error"
            if site.code == "hpo_fd6_counter_overflow"
            else "std::logic_error"
        )
        compare(site.args, args, f"{site.location} {site.code} source shape", failures)
        compare(site.family, family, f"{site.location} {site.code} family", failures)
        if site.message not in MESSAGES[site.code]:
            failures.append(f"{site.location}: changed legacy diagnostic {site.message!r}")
    for code, expected_messages in MESSAGES.items():
        actual = {site.message for site in sites if site.code == code}
        compare_members(
            actual, set(expected_messages), f"{code} source message variants", failures
        )


def catalog_contract(document: dict, sites: list[SourceSite], failures: list[str]):
    codes = document["codes"]
    source_codes = {site.code for site in sites}
    missing_source = source_codes - codes.keys()
    if missing_source:
        failures.append("catalog missing source codes: " + ", ".join(sorted(missing_source)))
    missing = MESSAGES.keys() - codes.keys()
    if missing:
        failures.append("catalog missing required FD6 codes: " + ", ".join(sorted(missing)))
    actual_fd6 = {code for code in codes if code.startswith(PREFIX)}
    compare_members(actual_fd6, set(MESSAGES), "catalog FD6 code set", failures)

    legacy = {
        "schema": document["schema"],
        "codes": {code: entry for code, entry in codes.items() if not code.startswith(PREFIX)},
    }
    compare(len(legacy["codes"]), 27, "unrelated catalog entry count", failures)
    legacy_bytes = (json.dumps(legacy, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    compare(
        hashlib.sha256(legacy_bytes).hexdigest(),
        LEGACY_CATALOG_SHA256,
        "unrelated catalog entries",
        failures,
    )

    # Bind each actual catalog rendering to its exact, unchanged legacy variant.
    renderings = {}
    for code, messages in MESSAGES.items():
        if code not in codes:
            continue
        entry = codes[code]
        compare(
            set(entry),
            {"class", "retryable", "args", "templates", "since"},
            f"{code} fields",
            failures,
        )
        classification = (
            "internal"
            if code == "hpo_fd6_internal"
            else "resource"
            if code == "hpo_fd6_counter_overflow"
            else "request"
        )
        compare(entry["class"], classification, f"{code} class", failures)
        compare(entry["retryable"], False, f"{code} retryable", failures)
        compare(entry["since"], "1.13.0", f"{code} since", failures)
        args = {"trial_id": {"kind": "integer"}} if code in ID_MESSAGES else {}
        compare(entry["args"], args, f"{code} argument declarations", failures)
        templates = entry["templates"]
        compare(len(templates), len(messages), f"{code} template variant count", failures)
        seen = []
        for template in templates:
            need(isinstance(template, str), f"{code}: non-string English template")
            parts = list(string.Formatter().parse(template))
            fields = [
                (field, spec, conversion)
                for _, field, spec, conversion in parts
                if field is not None
            ]
            expected_fields = [("trial_id", "", None)] if args else []
            compare(fields, expected_fields, f"{code} template fields", failures)
            candidates = [
                message
                for message in messages
                if (template.startswith(message) if args else template == message)
            ]
            if len(candidates) != 1:
                failures.append(f"{code}: template loses legacy variant: {template!r}")
                continue
            message = candidates[0]
            if args and "{trial_id}" not in template[len(message):]:
                failures.append(f"{code}: template needs a plain trial_id rendering suffix")
            seen.append(message)
            renderings[code, message] = template
        compare(sorted(seen), sorted(messages), f"{code} rendered legacy variants", failures)
    return renderings


def forward_child(stdout: bytes, stderr: bytes) -> None:
    sys.stdout.buffer.write(stdout)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(stderr)
    sys.stderr.buffer.flush()


def native_observations(native: Path):
    child = subprocess.run([str(native)], capture_output=True, check=False)
    forward_child(child.stdout, child.stderr)
    print(f"FD6 native raw returncode={child.returncode}", file=sys.stderr, flush=True)
    if child.returncode != 0:
        raise ChildFailure(child.returncode)
    document = parse_json(child.stdout)
    need(
        isinstance(document, dict) and set(document) == {"schema", "observations"},
        "native JSON must contain exactly schema and observations",
    )
    need(
        document["schema"] == "pineforge-hpo-fd6-observations/v1",
        "wrong native observation schema",
    )
    rows = document["observations"]
    need(isinstance(rows, list) and bool(rows), "empty/non-list observations")
    keys = {
        "case", "input_id", "code", "origin", "args", "what",
        "standard_family", "standard_what",
    }
    for index, row in enumerate(rows):
        need(isinstance(row, dict) and set(row) == keys, f"malformed observation {index}")
        for key in keys - {"input_id", "args"}:
            need(isinstance(row[key], str), f"observation {index}: {key} is not a string")
        input_id = row["input_id"]
        need(
            input_id is None or (type(input_id) is int and 0 <= input_id <= UINT64_MAX),
            f"observation {index}: invalid input_id",
        )
        need(isinstance(row["args"], dict), f"observation {index}: args is not an object")
        for key, argument in row["args"].items():
            need(
                isinstance(argument, dict)
                and set(argument) == {"type", "value"}
                and isinstance(argument["type"], str),
                f"observation {index}: malformed typed argument {key}",
            )
    return rows


def runtime_contract(rows, sites, renderings, failures: list[str]) -> None:
    expected = {}
    for case, (code, variant) in CASES.items():
        ids = (
            (None,)
            if case == "finalized_flush"
            else (0, 17, (1 << 63) + 17, UINT64_MAX - 1)
            if case == "below_floor"
            else IDS
        )
        for input_id in ids:
            expected[case, input_id] = code, MESSAGES[code][variant]
    observed = set()
    signatures = set()
    source_signatures = {site.signature() for site in sites}
    shapes = defaultdict(set)
    for row in rows:
        key = row["case"], row["input_id"]
        label = f"runtime {key[0]} input_id={key[1]}"
        if key in observed:
            failures.append(f"{label}: duplicate observation")
        observed.add(key)
        shape = tuple(sorted((name, arg["type"]) for name, arg in row["args"].items()))
        shapes[row["code"]].add(shape)
        signature = row["code"], row["what"], shape, row["standard_family"]
        signatures.add(signature)
        if signature not in source_signatures:
            failures.append(f"{label}: actual exception has no matching source site")
        if key not in expected:
            failures.append(f"{label}: unexpected observation")
            continue
        code, message = expected[key]
        compare(row["code"], code, f"{label} code", failures)
        compare(row["origin"], "hpo", f"{label} origin", failures)
        compare(row["what"], message, f"{label} legacy what()", failures)
        compare(row["standard_what"], message, f"{label} standard what()", failures)
        family = "std::logic_error" if key[0] == "finalized_flush" else "std::invalid_argument"
        compare(row["standard_family"], family, f"{label} catch compatibility", failures)
        if code in ID_MESSAGES:
            compare(set(row["args"]), {"trial_id"}, f"{label} argument keys", failures)
            argument = row["args"].get("trial_id")
            if argument is not None:
                compare(argument["type"], "uint64", f"{label} trial_id type", failures)
                compare(argument["value"], key[1], f"{label} trial_id value", failures)
        else:
            compare(row["args"], {}, f"{label} no-argument shape", failures)

        template = renderings.get((row["code"], row["what"]))
        if template is not None:
            values = {name: arg["value"] for name, arg in row["args"].items()}
            try:
                rendered = template.format(**values)
                need(rendered.startswith(row["what"]), f"{label}: rendering lost legacy text")
                if code in ID_MESSAGES:
                    need(str(key[1]) in rendered, f"{label}: rendering lost exact unsigned ID")
            except (KeyError, ValueError, TypeError) as error:
                failures.append(f"{label}: catalog rendering failed: {error}")
    compare_members(observed, set(expected), "runtime case/ID coverage", failures)
    for code, variants in shapes.items():
        if len(variants) != 1:
            failures.append(f"runtime {code}: conflicting argument shapes {sorted(variants)!r}")
    for site in sites:
        if site.code not in STATIC_ONLY and site.signature() not in signatures:
            failures.append(f"{site.location}: source throw variant has no actual observation")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        native = required_file(args.native)
        document = parse_json(required_file(args.catalog).read_bytes())
        need(
            isinstance(document, dict)
            and set(document) == {"schema", "codes"}
            and isinstance(document["codes"], dict),
            "catalog must contain exactly schema and the real codes object",
        )
        canonical_validator()(document)
        sites = source_sites()
        # Missing catalog members must not prevent the real baseline child run.
        rows = native_observations(native)
        failures = []
        source_contract(sites, failures)
        renderings = catalog_contract(document, sites, failures)
        runtime_contract(rows, sites, renderings, failures)
        print(
            f"FD6 intake: {len(sites)} source literal sites; {len(rows)} actual observations; "
            "static only (not executed): hpo_fd6_internal, hpo_fd6_counter_overflow",
            file=sys.stderr,
        )
        if failures:
            for failure in failures:
                print(f"FAIL: {failure}", file=sys.stderr)
            return 1
        print("PASS: FD6 source/catalog/projector conformance", file=sys.stderr)
        return 0
    except ChildFailure as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return error.returncode if 0 < error.returncode < 256 else 1
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
