"""Require literal job environments for App-token minting; run in CI/cloud only."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

import yaml

TOKEN_ACTION = "actions/create-github-app-token"
SECRET_REFERENCE = re.compile(
    r"""\bsecrets\s*(?:\.\s*([A-Za-z_][A-Za-z_0-9]*)|\[\s*(['"])([^'"]+)\2\s*\])""",
    re.IGNORECASE,
)
SCRIPT_MINT = re.compile(
    r"\baccess_tokens\b|\bcreateAppAuth\b|\bcreateInstallationAccessToken\b"
    r"|\bget_installation_access_token\b|\bRS256\b|\bjwt\b",
    re.IGNORECASE,
)


class WorkflowLoader(yaml.BaseLoader):
    """Preserve Actions scalars and refuse ambiguous duplicate/merge keys."""

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key == "<<" or key in result:
                raise ValueError("non-scalar, merge or duplicate YAML key; human review required")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def guarded_path(path):
    """Refuse sensitive names before reading, including symlink destinations."""
    def candidates():
        yield path.absolute()
        yield path.resolve()

    for candidate in candidates():
        parts = tuple(part.casefold() for part in candidate.parts)
        forbidden = any(
            part in (".ssh", ".aws", ".codex") or part.endswith(".env")
            for part in parts
        )
        forbidden |= any(
            parts[index : index + 3] == (".config", "zsh", "keys")
            or parts[index : index + 2] == (".config", "pineforge")
            for index in range(len(parts))
        )
        if forbidden:
            raise ValueError(f"refusing prohibited path: {candidate}")
    return candidate


def strings(value, keys=(), ancestors=frozenset()):
    if isinstance(value, str):
        yield keys, value
    elif isinstance(value, (dict, list)):
        if id(value) in ancestors:
            raise ValueError("recursive YAML alias; human review required")
        ancestors = ancestors | {id(value)}
        items = value.items() if isinstance(value, dict) else enumerate(value)
        for key, child in items:
            yield from strings(child, keys + (str(key),), ancestors)


def private_key_name(name):
    normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
    return "privatekey" in normalized or "appkey" in normalized


def minting_reasons(job, workflow_env):
    reasons = []
    steps = job.get("steps", [])
    if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
        raise ValueError("invalid job steps; human review required")
    for step in steps:
        uses = step.get("uses", "")
        if isinstance(uses, str) and uses.partition("@")[0].casefold() == TOKEN_ACTION:
            reasons.append("actions/create-github-app-token")
        run = step.get("run", "")
        if isinstance(run, str) and SCRIPT_MINT.search(run):
            reasons.append("possible script App-token mint; human review required")
    for keys, value in strings({"env": workflow_env, "job": job}):
        references = list(SECRET_REFERENCE.finditer(value))
        if any(private_key_name(match[1] or match[3]) for match in references):
            reasons.append("private-key secret reference")
        elif any(private_key_name(key) for key in keys) and re.search(
            r"\bsecrets\b", value, re.IGNORECASE
        ):
            reasons.append("secret passed through a private-key field")
        elif re.search(r"\bsecrets\s*\[", value, re.IGNORECASE) and not references:
            reasons.append("dynamic secret lookup may be a private key; human review required")
    return sorted(set(reasons))


def literal_environment(job):
    environment = job.get("environment")
    if isinstance(environment, dict):
        environment = environment.get("name")
    return (
        isinstance(environment, str)
        and bool(environment.strip())
        and "${{" not in environment
        and "}}" not in environment
        and "\n" not in environment
        and "\r" not in environment
    )


def check_workflow(path):
    workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=WorkflowLoader)
    if not isinstance(workflow, dict) or not isinstance(workflow.get("jobs"), dict):
        raise ValueError("workflow jobs must be a mapping; human review required")
    errors = []
    for job_id, job in workflow["jobs"].items():
        if not isinstance(job, dict):
            raise ValueError("job must be a mapping; human review required")
        reasons = minting_reasons(job, workflow.get("env", {}))
        if not reasons:
            continue
        description = "; ".join(reasons)
        if not literal_environment(job):
            errors.append(
                f"{path.name}: job {job_id}: App-token minting requires a non-empty "
                f"literal environment (plain name or mapping name): {description}"
            )
        elif any("human review required" in reason for reason in reasons):
            print(f"{path.name}: job {job_id}: {description}", file=sys.stderr)
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workflows-dir", type=Path, default=Path(".github/workflows")
    )
    args = parser.parse_args(argv)
    try:
        directory = guarded_path(args.workflows_dir)
        paths = sorted(
            path for path in directory.iterdir() if path.suffix in (".yml", ".yaml")
        )
        if not paths:
            raise ValueError("no workflow YAML files found")
        print("Workflow files to read:", flush=True)
        for path in paths:
            print(f"  {path}", flush=True)
        # Validate the entire read set before opening any of its files.
        checked = [guarded_path(path) for path in paths]
        for original, resolved in zip(paths, checked):
            if original != resolved:
                print(f"  {original} -> {resolved}", flush=True)
        errors = []
        for path in checked:
            errors.extend(check_workflow(path))
        for error in errors:
            print(error, file=sys.stderr)
        return 1 if errors else 0
    except (OSError, UnicodeError, ValueError, yaml.YAMLError, RecursionError) as error:
        # YAML exception text may contain source values: do not echo it.
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        print(f"App-token environment preflight refused: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
