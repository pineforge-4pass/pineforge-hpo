"""Generate, check, or release-stamp the HPO failure catalog's immutable diff."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import string
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CATALOG = "python/pineforge_hpo/hpo_failure_codes.json"
OUTPUT = "python/pineforge_hpo/hpo_failure_codes_diff.json"
SCHEMA = "pineforge-hpo-failure-catalog/v1"
DIFF_SCHEMA = "pineforge-hpo-failure-catalog-diff/v1"
VERSION = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
CODE = re.compile(r"[a-z][a-z0-9_]{2,47}")


def git(repo: Path, *arguments: str) -> bytes:
    return subprocess.check_output(
        ["git", "-C", str(repo), *arguments], stderr=subprocess.PIPE
    )


def version_key(tag: str) -> tuple[int, ...]:
    match = VERSION.fullmatch(tag)
    if match is None:
        raise ValueError(f"not a release tag: {tag}")
    return tuple(map(int, match.groups()))


def previous_release(repo: Path, revision: str, exclude: str | None) -> str:
    tags = git(repo, "tag", "--merged", revision).decode().splitlines()
    releases = [tag for tag in tags if VERSION.fullmatch(tag) and tag != exclude]
    if not releases:
        raise ValueError("no previous release tag; fetch the repository's release tags")
    return max(releases, key=version_key)


def sha256(payload: bytes | None) -> str | None:
    return hashlib.sha256(payload).hexdigest() if payload is not None else None


def changes(before: object, after: object, prefix: str = "") -> list[dict]:
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            path = f"{prefix}.{key}" if prefix else key
            if key not in before or key not in after:
                result.append(
                    {
                        "path": path,
                        "beforePresent": key in before,
                        "afterPresent": key in after,
                        "before": before.get(key),
                        "after": after.get(key),
                    }
                )
            else:
                result.extend(changes(before[key], after[key], path))
        return result
    if before != after or type(before) is not type(after):
        return [
            {
                "path": prefix,
                "beforePresent": True,
                "afterPresent": True,
                "before": before,
                "after": after,
            }
        ]
    return []


def validate_catalog(document: dict) -> None:
    if document.get("schema") != SCHEMA or not isinstance(document.get("codes"), dict):
        raise ValueError("invalid HPO failure catalog schema")
    for code, entry in document["codes"].items():
        if not CODE.fullmatch(code) or not code.startswith("hpo_"):
            raise ValueError(f"invalid HPO code: {code}")
        if (
            not isinstance(entry.get("class"), str)
            or type(entry.get("retryable")) is not bool
        ):
            raise ValueError(f"invalid classification: {code}")
        version_key("v" + entry["since"])
        args = entry.get("args")
        if not isinstance(args, dict):
            raise ValueError(f"invalid argument declaration: {code}")
        for name, argument in args.items():
            if not CODE.fullmatch(name) or argument.get("kind") not in {
                "string",
                "integer",
                "number",
                "boolean",
                "null",
            }:
                raise ValueError(f"invalid argument: {code}.{name}")
            values = argument.get("values")
            if argument["kind"] == "string" and (
                not isinstance(values, list)
                or not values
                or any(not isinstance(value, str) for value in values)
                or len(set(values)) != len(values)
            ):
                raise ValueError(
                    f"string arguments need a closed vocabulary: {code}.{name}"
                )
        templates = entry.get("templates")
        if not isinstance(templates, list) or not templates:
            raise ValueError(f"missing English templates: {code}")
        for template in templates:
            names = {
                name for _, name, _, _ in string.Formatter().parse(template) if name
            }
            if names != args.keys():
                raise ValueError(f"template arguments differ: {code}")


def catalog_diff(
    before_bytes: bytes | None,
    after_bytes: bytes,
    from_tag: str,
    to_version: str | None = None,
) -> dict:
    before = json.loads(before_bytes) if before_bytes is not None else {}
    after = json.loads(after_bytes)
    validate_catalog(after)
    target = {"version": to_version}
    if to_version is None:
        target["unreleased"] = True
    target["catalogSha256"] = sha256(after_bytes)
    result = {
        "schema": DIFF_SCHEMA,
        "from": {
            "version": from_tag.removeprefix("v"),
            "tag": from_tag,
            "catalogSha256": sha256(before_bytes),
        },
        "to": target,
        "catalogSchema": {"before": before.get("schema"), "after": after["schema"]},
        "metadataChanges": changes(
            {
                key: value
                for key, value in before.items()
                if key not in {"schema", "codes"}
            },
            {
                key: value
                for key, value in after.items()
                if key not in {"schema", "codes"}
            },
        ),
        "added": [],
        "changed": [],
        "removed": [],
        "deprecated": [],
    }
    old_entries, new_entries = before.get("codes", {}), after["codes"]
    for code in sorted(old_entries.keys() | new_entries.keys()):
        if code not in old_entries:
            result["added"].append(
                {"code": code, "collection": "codes", "entry": new_entries[code]}
            )
        elif code not in new_entries:
            raise ValueError(f"stable failure codes cannot be removed: {code}")
        else:
            fields = changes(old_entries[code], new_entries[code])
            if fields:
                result["changed"].append(
                    {"code": code, "collection": "codes", "fields": fields}
                )
        entry = new_entries.get(code, {})
        if entry.get("deprecated") and not old_entries.get(code, {}).get("deprecated"):
            result["deprecated"].append(
                {
                    "code": code,
                    "collection": "codes",
                    "deprecatedSince": entry.get("deprecatedSince"),
                    "replacedBy": entry.get("replacedBy", []),
                    "legacyOnly": entry.get("legacyOnly", False),
                }
            )
    return result


def dump(document: dict) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--catalog", default=CATALOG)
    parser.add_argument("--output", default=OUTPUT)
    parser.add_argument("--from-tag")
    parser.add_argument("--release-version")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        repo = args.repo.resolve()
        output = repo / args.output
        stored = json.loads(output.read_bytes()) if output.exists() else None
        release = args.release_version
        if args.check and release is None and stored:
            release = stored["to"]["version"]
        if release is not None:
            version_key("v" + release)
        previous = previous_release(repo, "HEAD", "v" + release if release else None)
        if args.from_tag is not None and previous != args.from_tag:
            raise ValueError(f"from tag must be the previous release {previous}")
        if release is not None and version_key("v" + release) <= version_key(previous):
            raise ValueError("target version must be newer than the previous release")
        exists = git(repo, "ls-tree", previous, "--", args.catalog).strip()
        before = git(repo, "show", f"{previous}:{args.catalog}") if exists else None
        after = (repo / args.catalog).read_bytes()
        expected = dump(catalog_diff(before, after, previous, release))
        if args.check:
            if not output.exists() or output.read_bytes() != expected:
                raise ValueError(
                    "catalog diff is stale; run scripts/gen_catalog_diff.py"
                )
        else:
            output.write_bytes(expected)
        print(
            f"HPO failure catalog diff {'checked' if args.check else 'generated'}: "
            f"{previous} -> {release or 'unreleased'}; sha256={sha256(after)}"
        )
        return 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
    ) as error:
        parser.exit(1, f"catalog diff: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
