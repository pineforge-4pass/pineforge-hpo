"""Main-owned PR admission rules. Head workflows are data, never executable code."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode
from yaml.tokens import (
    AliasToken,
    AnchorToken,
    DocumentEndToken,
    DocumentStartToken,
    FlowMappingEndToken,
    FlowMappingStartToken,
    FlowSequenceEndToken,
    FlowSequenceStartToken,
    KeyToken,
    ScalarToken,
    TagToken,
)

ROOT = Path(__file__).resolve().parents[2]
SMALL = "gke-8cpu-16gb-amd64"
LARGE = "gke-16cpu-32gb-amd64"
SHA = re.compile(r"[0-9a-f]{40}")
BOTS = frozenset(("dependabot[bot]", "pineforge-app[bot]"))
HEAVY_JOBS = frozenset(
    (
        "release-notification",
        "native",
        "native-macos",
        "python",
        "memory-gate",
        "optuna",
    )
)
RESULT_JOBS = HEAVY_JOBS | {
    "admit",
    "lint",
    "light",
    "docs",
}
GKE_JOBS = {
    "lint": SMALL,
    "light": SMALL,
    "release-notification": SMALL,
    "native": LARGE,
    "python": LARGE,
    "memory-gate": LARGE,
    "optuna": LARGE,
    "docs": SMALL,
}


class IdentityRefusal(ValueError):
    """Only fixed identity diagnostics may be shown in public workflow logs."""


def refusal_reason(error):
    if isinstance(error, IdentityRefusal):
        return str(error)
    return "workflow or API validation failed"


def guarded(path):
    """Refuse prohibited names and symlinks before following or opening a path."""
    candidate = path.absolute()
    parts = tuple(part.casefold() for part in candidate.parts)
    if any(
        part in (".ssh", ".aws", ".codex") or part.endswith(".env") for part in parts
    ) or any(
        parts[index : index + 3] == (".config", "zsh", "keys")
        or parts[index : index + 2] == (".config", "pineforge")
        for index in range(len(parts))
    ):
        raise ValueError(f"refusing prohibited path: {candidate}")
    for component in reversed((candidate, *candidate.parents)):
        if component.is_symlink():
            raise ValueError(f"refusing symlink path: {component}")
    return path


def read_files(paths):
    checked = [guarded(path) for path in paths]
    print("FILES to read:", flush=True)
    for path in checked:
        print(f"  {path}", flush=True)
    return {path: path.read_text(encoding="utf-8") for path in checked}


def local_sources(root):
    directory = guarded(root / ".github")
    paths = [directory / "actionlint.yaml"]
    for parent in (directory / "workflows", directory / "actions"):
        parent = guarded(parent)
        if parent.exists():
            # Do not traverse symlink directories or read arbitrary action assets.
            for current, directories, files in os.walk(
                guarded(parent), followlinks=False
            ):
                for name in directories:
                    path = guarded(Path(current) / name)
                    if path.is_symlink():
                        raise ValueError(f"refusing symlink directory: {path}")
                for name in files:
                    path = Path(current) / name
                    guarded(path)
                    if path.suffix in (".yml", ".yaml"):
                        if path.is_symlink():
                            raise ValueError(f"refusing symlink file: {path}")
                        paths.append(path)
    return {
        path.relative_to(root).as_posix(): value
        for path, value in read_files(sorted(paths)).items()
    }


def stage_sources(sources, root):
    """Materialize data under validated names; never execute a fetched file."""
    destinations = {}
    for name, text in sources.items():
        components = name.split("/")
        if not (
            name == ".github/actionlint.yaml"
            or name.startswith((".github/workflows/", ".github/actions/"))
        ) or any(
            not re.fullmatch(r"[A-Za-z0-9._-]+", part) or part in (".", "..")
            for part in components
        ):
            raise ValueError("invalid staged workflow path")
        path = guarded(root.joinpath(*components))
        destinations[path] = text
    print("FILES to stage as data:", flush=True)
    for path in destinations:
        print(f"  {path}", flush=True)
    for path, text in destinations.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def lint_sources(sources, trusted, root):
    # A PR cannot change actionlint's label allowlist. Disable code linters:
    # actionlint parses fetched workflow data; no head script is executed.
    for text in sources.values():
        for token in yaml.scan(bounded_yaml(text), Loader=yaml.BaseLoader):
            if not isinstance(token, ScalarToken):
                continue
            value = token.value.strip()
            if value.startswith(("./", "../", "/", "~")):
                reference = Path(value)
                if (
                    reference.is_absolute()
                    or value.startswith("~")
                    or ".." in reference.parts
                ):
                    raise ValueError("file reference escapes staged workflow data")
                guarded(root / reference)
    stage_sources(
        {**sources, ".github/actionlint.yaml": trusted[".github/actionlint.yaml"]},
        root,
    )
    workflows = sorted(
        guarded(root / name)
        for name in sources
        if name.startswith(".github/workflows/") and name.endswith((".yml", ".yaml"))
    )
    if not workflows:
        raise ValueError("empty workflow inventory")
    config = guarded(root / ".github/actionlint.yaml")
    actionlint = os.environ.get("ACTIONLINT", "actionlint")
    if "/" in actionlint:
        guarded(Path(actionlint))
    print("FILES actionlint will read:", flush=True)
    for path in (config, *workflows):
        print(f"  {path}", flush=True)
    # Do not echo parser diagnostics containing untrusted workflow commands.
    subprocess.run(
        [
            actionlint,
            "-config-file",
            str(config),
            "-shellcheck=",
            "-pyflakes=",
            *(str(path) for path in workflows),
        ],
        cwd=guarded(root),
        check=True,
        capture_output=True,
    )


def bounded_yaml(text):
    """Apply text, indentation and flow bounds before composing YAML."""
    text = text.replace("\r\n", "\n")
    if re.search(
        r"[\x00-\x08\x0b-\x1f\x7f\x85\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff]",
        text,
    ):
        raise ValueError("unsupported YAML character")
    lines = text.split("\n")
    if len(lines) > 20000:
        raise ValueError("YAML line count exceeds bound")
    if any(len(line) > 4096 for line in lines):
        raise ValueError("YAML line length exceeds bound")
    scalar_indent = None
    stack = []
    for line in lines:
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if scalar_indent is not None:
            if not stripped or indent > scalar_indent:
                continue
            scalar_indent = None
        if re.match(r" *\t", line):
            raise ValueError("YAML tab indentation")
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"(?:- +)?\?(?: |$)", stripped):
            raise ValueError("YAML explicit key")
        while stack and indent <= stack[-1]:
            stack.pop()
        stack.append(indent)
        if len(stack) > 40:
            raise ValueError("YAML depth exceeds bound")
        if re.search(r": *[|>][+-]?(?: *#.*)?$", stripped):
            scalar_indent = indent
    flow_depth = 0
    for token in yaml.scan(text, Loader=yaml.BaseLoader):
        if isinstance(token, (FlowMappingStartToken, FlowSequenceStartToken)):
            flow_depth += 1
            if flow_depth > 40:
                raise ValueError("YAML flow depth exceeds bound")
        elif isinstance(token, (FlowMappingEndToken, FlowSequenceEndToken)):
            flow_depth -= 1
        if isinstance(token, ScalarToken) and token.style:
            spelling = text[token.start_mark.index : token.end_mark.index]
            is_key = re.compile(r"[ \t]*:").match(text, token.end_mark.index)
            if is_key and ("\\" in spelling or "\n" in spelling):
                raise ValueError("YAML quoted key is refused")
    return text


def parse_yaml(text):
    """Reject YAML differential mechanisms without interpreting scalar types."""
    text = bounded_yaml(text)
    flow_depth = 0
    previous = None
    for token in yaml.scan(text, Loader=yaml.BaseLoader):
        if isinstance(token, (FlowMappingStartToken, FlowSequenceStartToken)):
            flow_depth += 1
            if flow_depth > 40:
                raise ValueError("YAML flow depth exceeds bound")
        elif isinstance(token, (FlowMappingEndToken, FlowSequenceEndToken)):
            flow_depth -= 1
        if isinstance(previous, KeyToken) and isinstance(token, ScalarToken):
            spelling = text[token.start_mark.index : token.end_mark.index]
            if token.style and ("\\" in spelling or "\n" in spelling):
                raise ValueError("YAML quoted key is refused")
        if isinstance(token, DocumentStartToken) and token.start_mark.line == 0:
            previous = token
            continue
        for kind, reason in (
            (AnchorToken, "anchor"),
            (AliasToken, "alias"),
            (TagToken, "tag"),
            (DocumentStartToken, "document"),
            (DocumentEndToken, "document"),
        ):
            if isinstance(token, kind):
                raise ValueError(f"YAML {reason} is refused")
        previous = token
    node = yaml.compose(text, Loader=yaml.BaseLoader)

    def convert(value, key=""):
        if isinstance(value, ScalarNode):
            return value.value
        if isinstance(value, SequenceNode):
            return [convert(child) for child in value.value]
        if not isinstance(value, MappingNode):
            raise ValueError("unreadable YAML node")
        if value.flow_style and key != "permissions":
            raise ValueError("YAML flow mapping is refused")
        result = {}
        for key_node, child in value.value:
            if not isinstance(key_node, ScalarNode):
                raise ValueError("YAML scalar key is required")
            spelling = text[key_node.start_mark.index : key_node.end_mark.index]
            if key_node.style and ("\\" in spelling or "\n" in spelling):
                raise ValueError("YAML quoted key is refused")
            name = key_node.value
            if name == "<<" or name in result:
                raise ValueError("YAML duplicate or merge key")
            if value.flow_style and (
                key_node.style or not isinstance(child, ScalarNode) or child.style
            ):
                raise ValueError("YAML plain permission scalars required")
            result[name] = convert(child, name)
        return result

    if node is None:
        raise ValueError("empty YAML")
    return convert(node)


def walk(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def check_pr_capabilities(data):
    for key, value in walk(data):
        if key in ("environment", "secrets") or (
            key == "permissions" and value not in ({}, {"contents": "read"})
        ):
            raise ValueError("credential capability is refused")
        expressions = (
            re.findall(r"\$\{\{(.*?)\}\}", value, flags=re.DOTALL)
            if isinstance(value, str)
            else []
        )
        if any(re.search(r"\bsecrets\b", item, re.IGNORECASE) for item in expressions):
            raise ValueError("credential reference is refused")
        if key == "uses" and value.startswith("./.github/workflows/"):
            raise ValueError("reusable workflow is refused")
        if key in ("run", "script") and any(
            re.search(
                r"\bgithub\s*(?:\.\s*(?:event|head_ref)\b|\[)", item, re.IGNORECASE
            )
            for item in expressions
        ):
            raise ValueError("PR interpolation in executable code")
        if key in ("run", "script") and re.search(r"\btimeout\s+(?:[0-9]|--)", value):
            raise ValueError("shell time limits are refused")
        if key == "continue-on-error":
            raise ValueError("continue-on-error weakens the result envelope")


def check_admission_contract(data):
    """Validate safety independently of the mutable main workflow envelope."""
    trigger = {
        "pull_request_target": {
            "branches": ["main"],
            "types": [
                "opened",
                "synchronize",
                "reopened",
                "ready_for_review",
                "converted_to_draft",
            ],
        }
    }
    if data.get("on") != trigger or data.get("permissions") != {}:
        raise ValueError("admission trigger/permission envelope differs")
    check_pr_capabilities(data)
    title = (
        "gke-pr #${{ github.event.pull_request.number }} "
        "${{ github.event.pull_request.head.sha }}"
    )
    if data.get("run-name") != title or data.get("concurrency") != {
        "group": "gke-pr-${{ github.event.pull_request.number }}",
        "cancel-in-progress": "true",
    }:
        raise ValueError("admission run binding envelope differs")
    runners = {
        **GKE_JOBS,
        "admit": "ubuntu-24.04",
        "native-macos": "macos-latest",
        "result": "ubuntu-24.04",
    }
    jobs = data.get("jobs", {})
    if [
        name
        for name, job in jobs.items()
        if re.search(r"\balways\s*\(", job.get("if", ""))
    ] != ["result"]:
        raise ValueError("result job-level always envelope differs")
    if set(jobs) != set(runners):
        raise ValueError("admission job inventory envelope differs")
    for name, job in jobs.items():
        if job.get("runs-on") != runners[name]:
            raise ValueError("runs-on envelope differs")
        if job.get("permissions") not in ({}, {"contents": "read"}):
            raise ValueError("credential capability is refused")
        if name in HEAVY_JOBS:
            condition = (
                "${{ needs.admit.outputs.allowed == 'true' "
                "&& needs.admit.outputs.docs_only != 'true' }}"
            )
            if job.get("needs") != ["admit"] or job.get("if") != condition:
                raise ValueError("admission proof envelope differs")
        elif name in GKE_JOBS:
            if (
                job.get("needs") != ["admit"]
                or job.get("if") != "${{ needs.admit.outputs.allowed == 'true' }}"
            ):
                raise ValueError("admission unconditional proof envelope differs")
        steps = job.get("steps", [])
        checkouts = [
            step
            for step in steps
            if step.get("uses", "").startswith("actions/checkout@")
        ]
        if len(checkouts) != 1:
            raise ValueError("checkout count envelope differs")
        for checkout in checkouts:
            expected = (
                "${{ github.sha }}"
                if name in ("admit", "result")
                else "${{ needs.admit.outputs.head_sha }}"
            )
            fields = checkout.get("with", {})
            if (
                fields.get("ref") != expected
                or fields.get("repository") != "${{ github.repository }}"
                or fields.get("persist-credentials") != "false"
            ):
                raise ValueError("checkout trust envelope differs")
        if name in ("admit", "result"):
            expected_command = f"python3 -I tests/release/pr_admission.py {name}"
            if not steps or steps[-1].get("run") != expected_command:
                raise ValueError(f"{name} boundary envelope differs")
    admission_outputs = {
        key: f"${{{{ steps.admit.outputs.{key} }}}}"
        for key in ("allowed", "head_sha", "base_sha", "docs_only", "code")
    }
    if (
        jobs["admit"].get("outputs") != admission_outputs
        or jobs["admit"]["steps"][-1].get("id") != "admit"
    ):
        raise ValueError("admission output envelope differs")
    result = jobs["result"]
    dependencies = result.get("needs", [])
    if (
        result.get("name") != "build-gate"
        or result.get("if") != "always()"
        or not isinstance(dependencies, list)
        or len(dependencies) != len(RESULT_JOBS)
        or set(dependencies) != RESULT_JOBS
    ):
        raise ValueError("result envelope differs")
    if result["steps"][-1].get("env", {}).get("NEEDS") != "${{ toJSON(needs) }}":
        raise ValueError("result inventory transport envelope differs")
    first = result["steps"][0]
    refusal = (
        "set -euo pipefail\n"
        'test "$ADMIT_RESULT" = success\n'
        'test "$ALLOWED" = true\n'
    )
    if (
        first.get("if") is not None
        or first.get("env") != {
            "ADMIT_RESULT": "${{ needs.admit.result }}",
            "ALLOWED": "${{ needs.admit.outputs.allowed }}",
        }
        or first.get("run") != refusal
    ):
        raise ValueError("result authorization envelope differs")


def check_post_merge_contract(data):
    """Post-merge proofs run on every push to main and every dispatch."""
    triggers = data.get("on", {})
    if set(triggers) != {"push", "workflow_dispatch"} or triggers.get("push") != {
        "branches": ["main"]
    }:
        raise ValueError("post-merge trigger envelope differs")
    if "changes" in data.get("jobs", {}):
        raise ValueError("post-merge classification is refused")
    for key, value in walk(data):
        if key == "needs" and (
            value == "changes" or isinstance(value, list) and "changes" in value
        ):
            raise ValueError("post-merge classification is refused")
        if (
            key == "outputs"
            and isinstance(value, dict)
            and {"docs_only", "workflows_only", "code"} & value.keys()
        ):
            raise ValueError("post-merge classification is refused")
        if isinstance(value, str):
            dotted = re.sub(
                r"\[\s*(['\"])([A-Za-z_][A-Za-z_0-9-]*)\1\s*\]", r".\2", value
            )
            if re.search(
                r"\bneeds\s*\.\s*changes\b|"
                r"\boutputs\s*\.\s*(?:docs_only|workflows_only|code)\b",
                dotted,
                re.IGNORECASE,
            ):
                raise ValueError("post-merge classification is refused")


def check_sources(sources, trusted):
    """Only fetched .github data influences rules; trusted defines the envelope."""
    parsed = {name: parse_yaml(text) for name, text in sources.items()}
    baseline = {name: parse_yaml(text) for name, text in trusted.items()}
    if set(parsed) != set(baseline):
        raise ValueError(
            "workflow inventory differs from main; update the allow table first"
        )
    if ".github/workflows/gke-pr.yml" not in parsed:
        raise ValueError("admission workflow is missing")
    check_admission_contract(parsed[".github/workflows/gke-pr.yml"])
    for name, data in parsed.items():
        for key, value in walk(data):
            if (
                key == "uses"
                and isinstance(value, str)
                and value.casefold().startswith("actions/cache")
            ):
                raise ValueError("Actions cache is refused")
            if key.casefold() == "cache":
                raise ValueError("setup cache is refused")
            if key == "runs-on" and not isinstance(value, str):
                raise ValueError("runs-on must be a bare literal scalar")
            if key == "runs-on" and not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
                raise ValueError("runs-on must be a bare literal scalar")
            if key in ("timeout-minutes",):
                raise ValueError("time limits are refused")
        if name == ".github/actionlint.yaml":
            if data != baseline[name]:
                raise ValueError("runner label envelope differs from main")
            continue
        if not name.startswith(".github/workflows/"):
            if data != baseline[name]:
                raise ValueError("action envelope differs from main")
            continue
        jobs = data.get("jobs", {})
        if name in (
            ".github/workflows/ci-post.yml",
            ".github/workflows/benchmark-smoke-post.yml",
        ):
            check_post_merge_contract(data)
        if any(
            event.startswith("pull_request") and event != "pull_request_target"
            for event in data.get("on", {})
        ) and any(
            str(job.get("runs-on", "")).startswith("gke-") for job in jobs.values()
        ):
            raise ValueError("pull_request workflows must not name GKE routing")
        original = baseline[name]
        if {key: value for key, value in data.items() if key != "jobs"} != {
            key: value for key, value in original.items() if key != "jobs"
        }:
            raise ValueError("workflow envelope differs from main")
        if set(jobs) != set(original.get("jobs", {})):
            raise ValueError("job envelope differs from main")
        admission = name.endswith("/gke-pr.yml")
        for job_id, job in jobs.items():
            old = original["jobs"][job_id]
            if admission or str(job.get("runs-on", "")).startswith("gke-"):
                check_pr_capabilities({"env": data.get("env", {}), "job": job})
            # Freeze guards, runners, permissions, dependencies, matrix and outputs.
            # Work commands remain editable by admitted members. Hosted boundary
            # scripts, legacy authz and aggregates are pinned in their entirety.
            frozen = job_id in (
                "admit",
                "authz",
                "changes",
                "result",
                "build-gate",
                "benchmark-check",
                "deploy",
            )
            actual = (
                job
                if frozen
                else {key: value for key, value in job.items() if key != "steps"}
            )
            expected = (
                old
                if frozen
                else {key: value for key, value in old.items() if key != "steps"}
            )
            if actual != expected:
                raise ValueError(f"{job_id}: job envelope differs from main")
            if not frozen:
                old_checkouts = [
                    step
                    for step in old.get("steps", [])
                    if step.get("uses", "").startswith("actions/checkout@")
                ]
                checkouts = [
                    step
                    for step in job.get("steps", [])
                    if step.get("uses", "").startswith("actions/checkout@")
                ]
                if checkouts != old_checkouts:
                    raise ValueError("checkout envelope differs from main")
            if admission:
                for step in job.get("steps", []):
                    uses = step.get("uses")
                    if uses and not re.fullmatch(
                        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}", uses
                    ):
                        raise ValueError("action must have an immutable SHA pin")


def check_identity(identity, live, permissions):
    repo = identity["repo"]
    if identity.get("head_repo") != repo:
        raise IdentityRefusal("head repository differs")
    if identity.get("fork") is not False:
        raise IdentityRefusal("fork refused")
    if identity.get("draft") is not False:
        raise IdentityRefusal("draft refused")
    if identity.get("base_ref") != "main":
        raise IdentityRefusal("base is not main")
    for key in ("base_sha", "head_sha"):
        if not SHA.fullmatch(identity.get(key, "")):
            raise IdentityRefusal("invalid payload SHA")
    if type(identity.get("number")) is not int or identity["number"] <= 0:
        raise IdentityRefusal("invalid PR number")
    for key in ("author", "actor", "triggering_actor"):
        login = identity.get(key)
        if not isinstance(login, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+(?:\[bot\])?", login
        ):
            raise IdentityRefusal("missing or invalid actor")
        if login not in BOTS and permissions.get(login) not in (
            "admin",
            "maintain",
            "write",
        ):
            raise IdentityRefusal("actor lacks write permission")
    if live.get("state") != "open":
        raise IdentityRefusal("live PR is not open")
    if live.get("draft") is not False:
        raise IdentityRefusal("live PR is a draft")
    if live.get("base", {}).get("ref") != "main":
        raise IdentityRefusal("live base differs")
    head = live.get("head", {})
    if head.get("sha") != identity["head_sha"]:
        raise IdentityRefusal("live head differs")
    if (
        head.get("repo", {}).get("full_name") != repo
        or head["repo"].get("fork") is not False
    ):
        raise IdentityRefusal("live head repository differs")
    if live.get("user", {}).get("login") != identity["author"]:
        raise IdentityRefusal("live author differs")


def tree_entries(tree):
    if tree.get("truncated") is not False or not isinstance(tree.get("tree"), list):
        raise ValueError("truncated or unreadable head tree")
    kept = []
    for row in tree["tree"]:
        path = row.get("path")
        if not isinstance(path, str):
            raise ValueError("unreadable head tree path")
        if not (
            path == ".github/actionlint.yaml"
            or path.startswith((".github/workflows/", ".github/actions/"))
        ):
            continue
        guarded(Path(path))
        if any(
            not re.fullmatch(r"[A-Za-z0-9._-]+", part) or part in (".", "..")
            for part in path.split("/")
        ):
            raise ValueError("head tree path is not a full match")
        if row.get("type") == "tree":
            continue
        if row.get("type") != "blob":
            raise ValueError("unsupported head tree type")
        if row.get("mode") not in ("100644", "100755"):
            raise ValueError("unsupported head tree mode")
        if type(row.get("size")) is not int or not 0 <= row["size"] <= 1024 * 1024:
            raise ValueError("head tree file size exceeds bound")
        if not SHA.fullmatch(row.get("sha", "")):
            raise ValueError("head blob SHA is invalid")
        kept.append(row)
    if len(kept) > 100:
        raise ValueError("head workflow count exceeds bound")
    if sum(row["size"] for row in kept) > 4 * 1024 * 1024:
        raise ValueError("head workflow total exceeds bound")
    if len({row["path"] for row in kept}) != len(kept):
        raise ValueError("duplicate head workflow path")
    return kept


def decode_blob(row, payload):
    if payload.get("encoding") != "base64":
        raise ValueError("unsupported head blob encoding")
    content = payload.get("content")
    if not isinstance(content, str):
        raise ValueError("unsupported head blob base64 content")
    data = base64.b64decode("".join(content.split()), validate=True)
    if len(data) != row["size"]:
        raise ValueError("head blob size mismatch")
    digest = hashlib.sha1(
        f"blob {len(data)}\0".encode("ascii") + data, usedforsecurity=False
    ).hexdigest()
    if digest != row["sha"]:
        raise ValueError("head blob SHA mismatch")
    return data.decode("utf-8")


def compare_paths(response, next_page=None):
    """Validate every compare page; only a short page proves completeness."""
    paths = []
    total, page = 0, 1
    while True:
        rows = response.get("files") if isinstance(response, dict) else None
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("compare page is unreadable or oversized")
        total += len(rows)
        if total >= 3000:
            raise ValueError("compare file list is capped")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("compare row is unreadable")
            if row.get("status") == "renamed" and "previous_filename" not in row:
                raise ValueError("compare rename has no origin")
            for key in ("filename", "previous_filename"):
                if key not in row and key == "previous_filename":
                    continue
                path = row.get(key)
                if (
                    not isinstance(path, str)
                    or "\0" in path
                    or any(part in ("", ".", "..") for part in path.split("/"))
                ):
                    raise ValueError("compare path is invalid")
                paths.append(path)
        if len(rows) < 100:
            if not paths:
                raise ValueError("compare file list is empty")
            return paths
        if next_page is None:
            raise ValueError("compare continuation is unreadable")
        page += 1
        try:
            response = next_page(page)
        except (
            KeyError,
            TypeError,
            ValueError,
            OSError,
            subprocess.CalledProcessError,
        ) as error:
            raise ValueError("compare page is unreadable") from error


def docs_only(paths):
    roots = {
        "README.md",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
        "CODE_OF_CONDUCT.md",
        "SECURITY.md",
        "LICENSE",
        "LICENSE.md",
    }
    return bool(paths) and all(
        path in roots
        or (
            path.startswith("docs/")
            and Path(path).name != "CMakeLists.txt"
            and Path(path).suffix in (".md", ".rst", ".txt")
        )
        for path in paths
    )


def judge(needs, paths, second_docs):
    if set(needs) != RESULT_JOBS:
        raise ValueError("result job inventory differs")
    admitted = needs["admit"].get("outputs", {})
    if admitted.get("allowed") != "true":
        raise ValueError("result admission did not allow")
    if admitted.get("docs_only") != str(second_docs).lower():
        raise ValueError("classification differs from admission")
    code = any(
        Path(path).suffix in (".cpp", ".hpp", ".h", ".c", ".cc", ".cmake", ".py", ".sh")
        or Path(path).name == "CMakeLists.txt"
        or (not path.startswith("docs/") and Path(path).suffix == ".txt")
        for path in paths
    )
    if second_docs and code:
        raise ValueError("code extension veto")
    for name, value in needs.items():
        expected = "skipped" if second_docs and name in HEAVY_JOBS else "success"
        if value.get("result") != expected:
            raise ValueError(f"result {name} must be {expected}")


def bound_run(runs, number, head):
    if (
        type(number) is not int
        or number <= 0
        or not isinstance(head, str)
        or not SHA.fullmatch(head)
    ):
        return False
    title = f"gke-pr #{number} {head}"
    candidates = [
        run
        for run in runs
        if run.get("path") == ".github/workflows/gke-pr.yml@refs/heads/main"
        and run.get("event") == "pull_request_target"
        and run.get("display_title") == title
        and type(run.get("id")) is int
    ]
    return (
        bool(candidates)
        and max(candidates, key=lambda run: run["id"]).get("conclusion") == "success"
    )


def api(endpoint):
    result = subprocess.run(
        ["gh", "api", endpoint], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


def identity_from_env():
    return {
        "repo": os.environ["REPO"],
        "head_repo": os.environ["HEAD_REPO"],
        "fork": json.loads(os.environ["HEAD_IS_FORK"]),
        "draft": json.loads(os.environ["DRAFT"]),
        "base_ref": os.environ["BASE_REF"],
        "base_sha": os.environ["BASE_SHA"],
        "head_sha": os.environ["HEAD_SHA"],
        "number": int(os.environ["PR_NUMBER"]),
        "author": os.environ["PR_AUTHOR"],
        "actor": os.environ["ACTOR"],
        "triggering_actor": os.environ["TRIGGERING_ACTOR"],
    }


def live_identity(identity):
    repo = identity["repo"]
    permissions = {}
    for key in ("author", "actor", "triggering_actor"):
        login = identity[key]
        if not re.fullmatch(r"[A-Za-z0-9_-]+(?:\[bot\])?", login):
            raise ValueError("invalid actor login")
        if login not in BOTS:
            permissions[login] = api(f"repos/{repo}/collaborators/{login}/permission")[
                "permission"
            ]
    live = api(f"repos/{repo}/pulls/{identity['number']}")
    check_identity(identity, live, permissions)


def output(values):
    path = guarded(Path(os.environ["GITHUB_OUTPUT"]))
    print(f"FILES output: {path}", flush=True)
    with path.open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            stream.write(f"{key}={value}\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("admit", "result", "check"))
    args = parser.parse_args(argv)
    try:
        trusted = local_sources(ROOT)
        if args.mode == "check":
            check_sources(trusted, trusted)
            return 0
        identity = identity_from_env()
        live_identity(identity)
        repo, head, base = (identity[key] for key in ("repo", "head_sha", "base_sha"))
        compare = f"repos/{repo}/compare/{base}...{head}?per_page=100&page="
        paths = compare_paths(
            api(compare + "1"), lambda page: api(compare + str(page))
        )
        cheap = docs_only(paths)
        if args.mode == "admit":
            rows = tree_entries(api(f"repos/{repo}/git/trees/{head}?recursive=1"))
            print("FILES head blobs to read as data:", flush=True)
            for row in rows:
                print(f"  {row['path']}", flush=True)
            sources = {}
            for row in rows:
                sources[row["path"]] = decode_blob(
                    row, api(f"repos/{repo}/git/blobs/{row['sha']}")
                )
            temporary = guarded(Path(os.environ["RUNNER_TEMP"]) / "admission-head")
            print(f"FILES admission scratch: {temporary}", flush=True)
            temporary.mkdir(parents=True, exist_ok=True)
            scratch = Path(tempfile.mkdtemp(prefix="head-", dir=temporary))
            lint_sources(sources, trusted, scratch)
            check_sources(sources, trusted)
            # Last operation: errors above cannot publish admission.
            output(
                {
                    "base_sha": base,
                    "head_sha": head,
                    "docs_only": str(cheap).lower(),
                    "code": str(not cheap).lower(),
                    "allowed": "true",
                }
            )
        else:
            judge(json.loads(os.environ["NEEDS"]), paths, cheap)
        print(json.dumps({"verdict": "PASS", "mode": args.mode, "head_sha": head}))
        return 0
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        yaml.YAMLError,
        subprocess.CalledProcessError,
    ) as error:
        # Parser/API exceptions may contain PR-written values; do not echo them.
        print(
            json.dumps(
                {
                    "verdict": "REFUSED",
                    "mode": args.mode,
                    "reason": refusal_reason(error),
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
