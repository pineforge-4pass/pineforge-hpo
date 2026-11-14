"""Static no-time-limits check. Source is parsed, never imported or executed.

The YAML reader deliberately accepts a bounded YAML subset: block/flow maps
and sequences, quoted/plain scalars, and literal/folded blocks. Unsupported
constructs (including aliases, tags and complex keys) are errors, never grants.
The dated policy uses JSON syntax, which is also YAML 1.2, with duplicate keys
rejected. No optional parser or runtime-package dependency is required.
"""

from __future__ import annotations

import ast
import configparser
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import tomllib


POLICY_PATH = "ci/no-time-limits-allow.yaml"
KINDS = frozenset(
    {
        "syntax-error",
        "unsupported-source",
        "python-subprocess-timeout",
        "python-wait-timeout",
        "python-async-timeout",
        "python-alarm",
        "python-poll-deadline",
        "python-elapsed-assertion",
        "python-timeout-option",
        "python-dynamic-options",
        "python-dynamic-code",
        "pytest-timeout",
        "shell-timeout",
        "cmake-timeout",
        "workflow-job-timeout",
        "workflow-step-timeout",
        "workflow-timeout",
        "native-wait-timeout",
        "native-alarm",
        "native-elapsed-assertion",
        "native-poll-deadline",
    }
)
TIME_OPTION = re.compile(
    r"(?:^|_)(?:timeout|deadline)(?:_|$)|"
    r"^(?:max_wall|wall_grace|time_limit)(?:_|$)"
)
ELAPSED_NAME = re.compile(r"(?:elapsed|deadline|expires|duration|stop_at|end_time)")
UNKNOWN = object()


def relative_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or "\0" in name
        or PurePosixPath(name).is_absolute()
        or any(part in ("", ".", "..") for part in name.split("/"))
    ):
        raise ValueError("invalid repository-relative name")
    return name


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    kind: str
    text: str
    column: int = 0

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.kind}: {self.text}"


def validate_findings(name: str, text: str, findings: list[Finding]) -> list[Finding]:
    """Refuse malformed scanner output instead of filtering it out."""
    relative_name(name)
    lines = text.splitlines()
    if not isinstance(findings, list):
        raise ValueError("finding data must be a list")
    for finding in findings:
        if (
            type(finding) is not Finding
            or finding.file != name
            or type(finding.line) is not int
            or not 1 <= finding.line <= len(lines)
            or finding.kind not in KINDS
            or type(finding.column) is not int
            or finding.column < 0
            or finding.text != lines[finding.line - 1].strip()
        ):
            raise ValueError("malformed finding data")
    return sorted(findings, key=lambda item: (item.line, item.column, item.kind))


class SourceError(ValueError):
    def __init__(self, line: int, message: str):
        super().__init__(message)
        self.line = line


@dataclass
class YNode:
    value: object
    line: int
    column: int = 0
    marks: dict[str, tuple[int, int]] = field(default_factory=dict)


def _uncomment(text: str) -> str:
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\" and quote == '"':
                index += 2
                continue
            if char == quote:
                if quote == "'" and text[index : index + 2] == "''":
                    index += 2
                    continue
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or text[index - 1].isspace()):
            return text[:index].rstrip()
        index += 1
    return text.rstrip()


def _mapping_colon(text: str) -> int:
    quote = ""
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\" and quote == '"':
                index += 2
                continue
            if char == quote:
                if quote == "'" and text[index : index + 2] == "''":
                    index += 2
                    continue
                quote = ""
        elif char in "'\"":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == ":" and depth == 0 and (
            index + 1 == len(text) or text[index + 1].isspace()
        ):
            return index
        index += 1
    return -1


class _Flow:
    def __init__(self, text: str, line: int, column: int = 0):
        self.text, self.line, self.column, self.index = text, line, column, 0

    def mark(self) -> tuple[int, int]:
        before = self.text[: self.index]
        return (
            self.line + before.count("\n"),
            len(before.rsplit("\n", 1)[-1])
            + (self.column if "\n" not in before else 0),
        )

    def space(self) -> None:
        while self.index < len(self.text) and self.text[self.index].isspace():
            self.index += 1

    def fail(self, message: str) -> None:
        raise SourceError(self.mark()[0], message)

    def value(self, key: bool = False) -> YNode:
        self.space()
        line, column = self.mark()
        if self.index == len(self.text):
            self.fail("missing YAML value")
        char = self.text[self.index]
        if char in "&*!?":
            self.fail("unsupported YAML alias, tag or complex key")
        if char in "[{":
            sequence = char == "["
            closing = "]" if sequence else "}"
            self.index += 1
            items = [] if sequence else {}
            marks = {}
            self.space()
            while self.index < len(self.text) and self.text[self.index] != closing:
                item = self.value(key=not sequence)
                if sequence:
                    items.append(item)
                else:
                    if not isinstance(item.value, str):
                        self.fail("non-scalar YAML key")
                    self.space()
                    if self.index >= len(self.text) or self.text[self.index] != ":":
                        self.fail("missing flow mapping colon")
                    self.index += 1
                    if item.value in items:
                        raise SourceError(item.line, "duplicate YAML key")
                    marks[item.value] = (item.line, item.column)
                    items[item.value] = self.value()
                self.space()
                if self.index < len(self.text) and self.text[self.index] == ",":
                    self.index += 1
                    self.space()
                elif self.index >= len(self.text) or self.text[self.index] != closing:
                    self.fail("missing flow separator")
            if self.index == len(self.text):
                self.fail("unterminated YAML flow collection")
            self.index += 1
            return YNode(items, line, column, marks)
        if char in "'\"":
            start = self.index
            self.index += 1
            while self.index < len(self.text):
                current = self.text[self.index]
                if current == "\\" and char == '"':
                    self.index += 2
                    continue
                if current == char:
                    if char == "'" and self.text[self.index : self.index + 2] == "''":
                        self.index += 2
                        continue
                    self.index += 1
                    token = self.text[start : self.index]
                    try:
                        value = (
                            json.loads(token)
                            if char == '"'
                            else token[1:-1].replace("''", "'")
                        )
                    except ValueError as error:
                        raise SourceError(
                            line, "unsupported quoted YAML scalar"
                        ) from error
                    return YNode(value, line, column)
                self.index += 1
            self.fail("unterminated quoted YAML scalar")
        start = self.index
        while self.index < len(self.text):
            current = self.text[self.index]
            if current in ",]}" or (key and current == ":"):
                break
            self.index += 1
        value = self.text[start : self.index].strip()
        if not value or value == "<<":
            self.fail("unsupported YAML scalar or merge")
        return YNode(value, line, column)

    def parse(self) -> YNode:
        value = self.value()
        self.space()
        if self.index != len(self.text):
            self.fail("trailing YAML scalar data")
        return value


@dataclass
class _YLine:
    indent: int
    text: str
    number: int
    block: YNode | None = None


def parse_yaml(text: str) -> YNode:
    """Build nodes and key marks before making any job/step scope decision."""
    raw = text.splitlines()
    rows = []
    index = 0
    while index < len(raw):
        number = index + 1
        original = raw[index]
        if "\t" in original[: len(original) - len(original.lstrip())]:
            raise SourceError(number, "tabs in YAML indentation")
        indent = len(original) - len(original.lstrip(" "))
        body = _uncomment(original[indent:])
        index += 1
        if not body or body == "---":
            continue
        if body == "...":
            raise SourceError(number, "multiple/document-end YAML is unsupported")
        entry = body[2:] if body.startswith("- ") else body
        colon = _mapping_colon(entry)
        value = entry[colon + 1 :].strip() if colon >= 0 else entry
        block = None
        if re.fullmatch(r"[|>][+-]?", value):
            start = index
            key_indent = indent + (2 if body.startswith("- ") else 0)
            while index < len(raw) and (
                not raw[index].strip()
                or len(raw[index]) - len(raw[index].lstrip(" ")) > key_indent
            ):
                index += 1
            content = raw[start:index]
            depths = [
                len(line) - len(line.lstrip(" ")) for line in content if line.strip()
            ]
            depth = min(depths) if depths else indent + 1
            content = [line[depth:] if line.strip() else "" for line in content]
            if value.startswith(">"):
                # A folded block joins equally indented nonempty lines. Preserve
                # more-indented lines and paragraph boundaries.
                folded = ""
                for pos, line in enumerate(content):
                    folded += line
                    following = content[pos + 1] if pos + 1 < len(content) else ""
                    folded += (
                        " "
                        if line and following and not line[0].isspace()
                        and not following[0].isspace()
                        else "\n"
                    )
                rendered = folded
            else:
                rendered = "\n".join(content) + ("\n" if content else "")
            if value.endswith("-"):
                rendered = rendered.rstrip("\n")
            block = YNode(rendered, start + 1, depth)
        elif value.startswith(("{", "[")):
            # Gather flow collections structurally; unfinished syntax never
            # degrades to an indentation-based job allowance.
            while True:
                try:
                    _Flow(value, number).parse()
                    break
                except SourceError as error:
                    if "unterminated" not in str(error) or index == len(raw):
                        raise
                    value += "\n" + _uncomment(raw[index])
                    index += 1
            prefix = body[: body.index(entry) + colon + 1] if colon >= 0 else ""
            body = prefix + " " + value if prefix else value
        rows.append(_YLine(indent, body, number, block))
    if not rows:
        raise SourceError(1, "empty YAML document")
    cursor = 0

    def scalar(value: str, row: _YLine, column: int) -> YNode:
        if value.startswith(("{", "[", "'", '"', "&", "*", "!", "?")):
            return _Flow(value, row.number, column).parse()
        if value == "<<":
            raise SourceError(row.number, "YAML merges are unsupported")
        return YNode(value, row.number, column)

    def block(indent: int) -> YNode:
        nonlocal cursor
        start = rows[cursor]
        sequence = start.text == "-" or start.text.startswith("- ")
        result = [] if sequence else {}
        marks = {}
        while cursor < len(rows) and rows[cursor].indent == indent:
            row = rows[cursor]
            is_item = row.text == "-" or row.text.startswith("- ")
            if is_item != sequence:
                raise SourceError(row.number, "mixed YAML map and sequence")
            if sequence:
                rest = row.text[1:].strip()
                cursor += 1
                if rest:
                    # A sequence's inline first mapping entry is parsed in the
                    # same map as its following entries, at their real depth.
                    if _mapping_colon(rest) >= 0:
                        rows.insert(
                            cursor, _YLine(indent + 2, rest, row.number, row.block)
                        )
                        item = block(indent + 2)
                    else:
                        item = scalar(rest, row, indent + 2)
                elif cursor < len(rows) and rows[cursor].indent > indent:
                    item = block(rows[cursor].indent)
                else:
                    item = YNode("", row.number)
                result.append(item)
                continue
            colon = _mapping_colon(row.text)
            if colon < 0:
                raise SourceError(row.number, "expected YAML mapping entry")
            key = scalar(row.text[:colon].strip(), row, indent)
            if not isinstance(key.value, str) or not key.value or key.value == "<<":
                raise SourceError(row.number, "invalid YAML mapping key")
            if key.value in result:
                raise SourceError(row.number, "duplicate YAML key")
            marks[key.value] = (row.number, indent)
            rest = row.text[colon + 1 :].strip()
            cursor += 1
            if row.block is not None:
                item = row.block
            elif rest:
                item = scalar(rest, row, indent + colon + 1)
            elif cursor < len(rows) and rows[cursor].indent > indent:
                item = block(rows[cursor].indent)
            else:
                item = YNode("", row.number)
            result[key.value] = item
        if cursor < len(rows) and rows[cursor].indent > indent:
            raise SourceError(rows[cursor].number, "unexpected YAML nesting")
        return YNode(result, start.number, indent, marks)

    if rows[0].text.startswith(("{", "[")):
        if len(rows) != 1:
            raise SourceError(rows[1].number, "trailing YAML document")
        return _Flow(rows[0].text, rows[0].number).parse()
    document = block(rows[0].indent)
    if cursor != len(rows):
        raise SourceError(rows[cursor].number, "unexpected YAML indentation")
    return document


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class AllowJob:
    workflow: str
    job: str
    purpose: str
    timeout_minutes: int


@dataclass(frozen=True)
class Policy:
    entries: tuple[AllowJob, ...]


def parse_policy(text: str, *, allow_control: bool = False) -> Policy:
    """Only measured, dated exact CD identities can grant a job-level bound."""
    data = json.loads(text, object_pairs_hook=unique_object)
    fields = {"version", "reviewed_on", "authority", "data", "jobs"}
    if not isinstance(data, dict) or set(data) != fields:
        raise ValueError("policy keys differ")
    if type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("policy version differs")
    if data["authority"] != "owner 11-06 CI rule":
        raise ValueError("policy authority differs")
    if data["data"] != "production" and not (
        allow_control and data["data"] == "control"
    ):
        raise ValueError("control DATA cannot grant a production exception")
    reviewed = date.fromisoformat(data["reviewed_on"])
    if reviewed.isoformat() != data["reviewed_on"] or reviewed > date.today():
        raise ValueError("invalid policy date")
    if not isinstance(data["jobs"], list):
        raise ValueError("policy jobs must be a list")
    entries = []
    identities = set()
    for row in data["jobs"]:
        if not isinstance(row, dict) or set(row) != {
            "workflow", "job", "purpose", "measured_on", "measured_max_seconds",
            "margin_seconds", "timeout_minutes", "evidence",
        }:
            raise ValueError("measured job keys differ")
        workflow = relative_name(row["workflow"])
        if (
            not re.fullmatch(r"\.github/workflows/[\w.-]+\.ya?ml", workflow)
            or not re.fullmatch(r"[A-Za-z_][\w-]*", row["job"])
        ):
            raise ValueError("policy requires exact workflow and job")
        if row["purpose"] not in {"release", "deploy", "rollback", "migrate"}:
            raise ValueError("policy purpose must be CD")
        measured = date.fromisoformat(row["measured_on"])
        if measured.isoformat() != row["measured_on"] or measured > reviewed:
            raise ValueError("invalid measurement date")
        for key in ("measured_max_seconds", "margin_seconds", "timeout_minutes"):
            if type(row[key]) is not int or row[key] <= 0:
                raise ValueError("positive integer measurement and margin required")
        expected = (row["measured_max_seconds"] + row["margin_seconds"] + 59) // 60
        if row["timeout_minutes"] != expected:
            raise ValueError("job limit must equal measured maximum plus margin")
        if not isinstance(row["evidence"], str) or not row["evidence"].strip():
            raise ValueError("measurement evidence required")
        if data["data"] == "production" and re.search(
            r"\b(?:synthetic|control data|fixture)\b", row["evidence"], re.I
        ):
            raise ValueError("synthetic measurements are control DATA only")
        identity = (workflow, row["job"])
        if identity in identities:
            raise ValueError("duplicate policy job")
        identities.add(identity)
        entries.append(AllowJob(workflow, row["job"], row["purpose"], expected))
    return Policy(tuple(entries))


class _Scan:
    def __init__(self, name: str, text: str, policy: Policy):
        relative_name(name)
        if type(policy) is not Policy:
            raise ValueError("missing or malformed policy")
        self.name, self.text, self.policy = name, text, policy
        self.lines = text.splitlines()
        self.findings: list[Finding] = []
        self.allowed: list[tuple[str, str]] = []

    def add(self, line: int, kind: str, column: int = 0) -> None:
        if not self.lines or not 1 <= line <= len(self.lines):
            raise ValueError("scanner produced an invalid source mark")
        self.findings.append(
            Finding(self.name, line, kind, self.lines[line - 1].strip(), column)
        )

    def embedded(self, text: str, language: str, line: int) -> None:
        child = _Scan(self.name, text, self.policy)
        child.parse(language)
        for finding in validate_findings(self.name, text, child.findings):
            # Escaped strings can encode many lines on one source line. The
            # execution sink is the auditable physical mark in the outer file.
            self.add(line, finding.kind, finding.column)

    def parse(self, language: str) -> None:
        try:
            if language == "python":
                _Python(self).visit(ast.parse(self.text, filename=self.name))
            elif language == "yaml":
                _workflow(self, parse_yaml(self.text))
            elif language == "shell":
                _shell(self, self.text)
            elif language == "cmake":
                _cmake(self)
            elif language == "native":
                _native(self)
            elif language == "config":
                _config(self)
            elif language == "presets":
                _presets(self)
            else:
                self.add(1, "unsupported-source")
        except SyntaxError as error:
            self.add(error.lineno or 1, "syntax-error")
        except SourceError as error:
            self.add(error.line, "syntax-error")
        except (configparser.Error, tomllib.TOMLDecodeError, json.JSONDecodeError):
            self.add(1, "syntax-error")


@dataclass(frozen=True)
class _Ref:
    name: str


@dataclass(frozen=True)
class _Clock:
    pass


@dataclass(frozen=True)
class _File:
    name: str


class _PythonBindings(ast.NodeVisitor):
    def __init__(self, scan: _Scan):
        self.scan = scan
        self.scopes: list[dict[str, object]] = [{}]
        self.poll_tests: set[int] = set()
        self.generated: dict[str, object] = {}
        self.executors: set[str] = set()

    def visit_Module(self, node: ast.Module) -> None:
        # Infer local launch helpers from their real callees, including methods
        # such as run_cli. Fixture/data helpers acquire no execution semantics
        # from their filename or from an argument that happens to mention time.
        for statement in node.body:
            if isinstance(statement, ast.Import) or (
                isinstance(statement, ast.ImportFrom)
                and all(alias.name != "*" for alias in statement.names)
            ):
                self.visit(statement)
        functions = [
            item for item in ast.walk(node)
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        effects = {
            "subprocess.run", "subprocess.call", "subprocess.check_call",
            "subprocess.check_output", "subprocess.Popen", "os.system", "os.popen",
        }
        changed = True
        while changed:
            changed = False
            for function in functions:
                if function.name in self.executors:
                    continue
                if any(
                    isinstance(item, ast.Call)
                    and (
                        self.ref(item.func) in effects
                        or self.ref(item.func).rsplit(".", 1)[-1] in self.executors
                    )
                    for statement in function.body for item in ast.walk(statement)
                ):
                    self.executors.add(function.name)
                    changed = True
        self.generic_visit(node)

    def lookup(self, name: str) -> object:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return UNKNOWN

    def ref(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            value = self.lookup(node.id)
            return value.name if isinstance(value, _Ref) else node.id
        if isinstance(node, ast.Attribute):
            value = self.value(node.value)
            prefix = value.name if isinstance(value, _Ref) else self.ref(node.value)
            return prefix + "." + node.attr
        return ""

    def value(self, node: ast.AST) -> object:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return self.lookup(node.id)
        if isinstance(node, ast.Attribute):
            receiver = self.value(node.value)
            if node.attr == "join" and isinstance(receiver, str):
                return _Ref("str.join")
            if node.attr == "get" and isinstance(receiver, dict):
                return _Ref("dict.get")
            return _Ref(self.ref(node))
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self.value(item) for item in node.elts]
        if isinstance(node, ast.Dict):
            result = {}
            for key, value in zip(node.keys, node.values):
                if key is None:
                    more = self.value(value)
                    if not isinstance(more, dict):
                        return UNKNOWN
                    result.update(more)
                else:
                    name = self.value(key)
                    if not isinstance(name, str):
                        return UNKNOWN
                    result[name] = self.value(value)
            return result
        if isinstance(node, ast.BinOp):
            left, right = self.value(node.left), self.value(node.right)
            if isinstance(left, _Clock) or isinstance(right, _Clock):
                return _Clock()
            if isinstance(node.op, ast.Div) and isinstance(right, str):
                prefix = left.name if isinstance(left, _File) else (
                    left if isinstance(left, str) else self.ref(node.left)
                )
                return _File(prefix + "/" + right)
            if isinstance(node.op, ast.Add) and type(left) is type(right):
                if isinstance(left, (str, list)):
                    return left + right
            return UNKNOWN
        if isinstance(node, ast.Call):
            name = self.ref(node.func)
            if name == "pathlib.Path" and node.args:
                value = self.value(node.args[0])
                return value if isinstance(value, _File) else _File(
                    value if isinstance(value, str) else ast.dump(node.args[0])
                )
            if name == "str" and node.args:
                value = self.value(node.args[0])
                if isinstance(value, (str, _File)):
                    return value
            if self.is_clock(name):
                return _Clock()
            if name in {
                "queue.Queue", "queue.SimpleQueue", "queue.LifoQueue",
                "queue.PriorityQueue", "threading.Event", "threading.Thread",
                "threading.Condition", "threading.Semaphore", "subprocess.Popen",
                "multiprocessing.Process", "multiprocessing.Queue",
                "concurrent.futures.Future",
            }:
                return _Ref(name + "()")
            if name == "dict":
                result = {}
                if node.args:
                    initial = self.value(node.args[0])
                    if not isinstance(initial, dict):
                        return UNKNOWN
                    result.update(initial)
                for item in node.keywords:
                    if item.arg is None:
                        more = self.value(item.value)
                        if not isinstance(more, dict):
                            return UNKNOWN
                        result.update(more)
                    else:
                        result[item.arg] = self.value(item.value)
                return result
        return UNKNOWN

    @staticmethod
    def is_clock(name: str) -> bool:
        return name in {
            "time.monotonic", "time.monotonic_ns", "time.perf_counter",
            "time.perf_counter_ns", "time.time", "time.time_ns",
            "datetime.datetime.now", "datetime.datetime.utcnow",
        }

    def clocky(self, node: ast.AST) -> bool:
        if isinstance(self.value(node), _Clock):
            return True
        return any(
            (isinstance(item, ast.Name) and (
                ELAPSED_NAME.search(item.id)
                or isinstance(self.lookup(item.id), _Clock)
            ))
            or (isinstance(item, ast.Call) and self.is_clock(self.ref(item.func)))
            for item in ast.walk(node)
        )

    def add(self, node: ast.AST, kind: str) -> None:
        self.scan.add(node.lineno, kind, node.col_offset)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.scopes[-1][alias.asname or alias.name.split(".")[0]] = _Ref(
                alias.name if alias.asname else alias.name.split(".")[0]
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == "*":
                # Unknown imports must not hide the standard timeout functions.
                self.add(node, "python-dynamic-code")
            else:
                self.scopes[-1][alias.asname or alias.name] = _Ref(
                    (node.module + "." if node.module else "") + alias.name
                )

    def bind(self, target: ast.AST, value: object) -> None:
        if isinstance(target, ast.Name):
            self.scopes[-1][target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            for index, item in enumerate(target.elts):
                self.bind(
                    item, value[index]
                    if isinstance(value, list) and index < len(value) else UNKNOWN
                )
        elif isinstance(target, ast.Subscript):
            container, key = self.value(target.value), self.value(target.slice)
            if isinstance(container, dict) and isinstance(key, str):
                container[key] = value

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        value = self.value(node.value)
        # Assigning a function object retains its resolved import identity.
        if isinstance(node.value, ast.Name) and value is UNKNOWN:
            value = _Ref(self.ref(node.value))
        for target in node.targets:
            self.bind(target, value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
            self.bind(node.target, self.value(node.value))

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        left, right = self.value(node.target), self.value(node.value)
        if isinstance(node.op, ast.Add) and isinstance(right, list):
            self.bind(
                node.target, (left if isinstance(left, list) else [UNKNOWN]) + right
            )
        else:
            self.bind(node.target, UNKNOWN)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        self.scopes[-1][node.name] = _Ref(node.name)
        arguments = (
            *node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
        )
        local = {item.arg: UNKNOWN for item in arguments}
        for item in (node.args.vararg, node.args.kwarg):
            if item is not None:
                local[item.arg] = UNKNOWN
        self.scopes.append(local)
        generated = self.generated.copy()
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()
        self.generated = generated

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_If(self, node: ast.If) -> None:
        if (
            id(node.test) not in self.poll_tests
            and self.clocky(node.test)
            and any(
                isinstance(item, ast.Raise)
                for stmt in node.body for item in ast.walk(stmt)
            )
        ):
            self.add(node.test, "python-elapsed-assertion")
        self.visit(node.test)
        original = deepcopy(self.scopes[-1], {id(UNKNOWN): UNKNOWN})
        original_generated = self.generated.copy()
        for statement in node.body:
            self.visit(statement)
        left = deepcopy(self.scopes[-1], {id(UNKNOWN): UNKNOWN})
        left_generated = self.generated.copy()
        self.scopes[-1] = deepcopy(original, {id(UNKNOWN): UNKNOWN})
        self.generated = original_generated.copy()
        for statement in node.orelse:
            self.visit(statement)
        right = self.scopes[-1]
        right_generated = self.generated
        # A conditional None must never erase a finite value from another path.
        self.scopes[-1] = {
            key: left.get(key, UNKNOWN)
            if left.get(key, UNKNOWN) == right.get(key, UNKNOWN) else UNKNOWN
            for key in left.keys() | right.keys()
        }
        self.generated = {
            key: left_generated.get(key, UNKNOWN)
            if left_generated.get(key, UNKNOWN) == right_generated.get(key, UNKNOWN)
            else UNKNOWN
            for key in left_generated.keys() | right_generated.keys()
        }

    def poll(self, node: ast.While | ast.For) -> None:
        original = deepcopy(self.scopes[-1], {id(UNKNOWN): UNKNOWN})
        original_generated = self.generated.copy()
        if isinstance(node, ast.While) and self.clocky(node.test):
            self.poll_tests.add(id(node.test))
            self.add(node.test, "python-poll-deadline")
        for statement in node.body:
            for item in ast.walk(statement):
                if (
                    isinstance(item, ast.If) and self.clocky(item.test)
                    and any(
                        isinstance(control, (ast.Break, ast.Return, ast.Raise))
                        for branch in item.body for control in ast.walk(branch)
                    )
                ):
                    self.poll_tests.add(id(item.test))
                    self.add(item.test, "python-poll-deadline")
        self.generic_visit(node)
        after = self.scopes[-1]
        self.scopes[-1] = {
            key: original.get(key, UNKNOWN)
            if original.get(key, UNKNOWN) == after.get(key, UNKNOWN) else UNKNOWN
            for key in original.keys() | after.keys()
        }
        after_generated = self.generated
        self.generated = {
            key: original_generated.get(key, UNKNOWN)
            if original_generated.get(key, UNKNOWN) == after_generated.get(key, UNKNOWN)
            else UNKNOWN
            for key in original_generated.keys() | after_generated.keys()
        }


def _argv(scan: _Scan, words: list, line: int, column: int = 0) -> None:
    """Inspect actual command arguments, never arbitrary fixture string tokens."""
    if not words:
        return
    index = 0
    while index < len(words) and isinstance(words[index], str) and re.match(
        r"^[A-Za-z_]\w*=", words[index]
    ):
        index += 1
    while index < len(words):
        word = words[index]
        command = PurePosixPath(word).name if isinstance(word, str) else ""
        if command not in {"env", "command", "exec", "sudo", "nice", "stdbuf"}:
            break
        index += 1
        while index < len(words) and isinstance(words[index], str) and (
            words[index].startswith("-") or re.match(r"^[A-Za-z_]\w*=", words[index])
        ):
            index += 1
    if index == len(words):
        return
    name = PurePosixPath(words[index]).name if isinstance(words[index], str) else ""
    arguments = words[index + 1 :]
    if name in {"echo", "printf", "test", "[", "true", "false", "cat"}:
        return
    if name in {"timeout", "gtimeout"} or (
        name == "busybox" and arguments and arguments[0] == "timeout"
    ):
        scan.add(line, "shell-timeout", column)
    for position, argument in enumerate(arguments):
        if not isinstance(argument, str):
            continue
        flag = argument.split("=", 1)[0]
        limiting = (
            flag.startswith("-")
            and re.search(
                r"(?:timeout|deadline|time-limit|max-wall|wall-grace|max-time)",
                flag.replace("_", "-"),
            )
        ) or (name == "curl" and flag == "-m") or (
            name == "wget" and flag == "-T"
        )
        if name in {"pytest", "py.test"} and re.match(r"timeout\s*=", argument):
            limiting = True
        if limiting:
            value = argument.partition("=")[2] if "=" in argument else (
                arguments[position + 1] if position + 1 < len(arguments) else UNKNOWN
            )
            if name in {"pytest", "py.test"} and str(value) in {"0", "0.0"}:
                continue
            scan.add(line, "shell-timeout", column + position + 1)
    if "-c" in arguments:
        position = arguments.index("-c")
        code = arguments[position + 1] if position + 1 < len(arguments) else UNKNOWN
        language = "python" if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", name) else (
            "shell" if name in {"sh", "bash", "dash", "zsh"} else ""
        )
        if language:
            if isinstance(code, str):
                scan.embedded(code, language, line)
            else:
                scan.add(line, "python-dynamic-code", column)
    if name == "eval":
        if all(isinstance(argument, str) for argument in arguments):
            scan.embedded(" ".join(arguments), "shell", line)
        else:
            scan.add(line, "python-dynamic-code", column)


def _substitution_end(text: str, start: int, backtick: bool) -> int:
    index, depth, quote = start, 1, ""
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if backtick:
            if char == "`":
                return index
        elif quote:
            if char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if not depth:
                return index
        index += 1
    raise SourceError(text.count("\n", 0, start) + 1, "unterminated shell substitution")


def _shell_tokens(
    text: str,
    substitutions: list[tuple[str, int]] | None = None,
    *,
    expansion_data: bool = False,
) -> list[tuple[str, int, int]]:
    """Tokenize shell words with physical marks, quotes and continued lines."""
    tokens = []
    index, line, column = 0, 1, 0
    while index < len(text):
        char = text[index]
        if char in " \t\r":
            index, column = index + 1, column + 1
            continue
        if char == "\n":
            tokens.append(("\n", line, column))
            index, line, column = index + 1, line + 1, 0
            continue
        if char == "#":
            while index < len(text) and text[index] != "\n":
                index, column = index + 1, column + 1
            continue
        start_line, start_column = line, column
        if char in ";|&()<>":
            token = char
            index, column = index + 1, column + 1
            if index < len(text) and text[index] == char:
                token += char
                index, column = index + 1, column + 1
            tokens.append((token, start_line, start_column))
            continue
        word, quote = "", ""
        while index < len(text):
            char = text[index]
            if not quote and (char.isspace() or char in ";|&()<>"):
                break
            if quote != "'" and (
                text.startswith("$(", index) and not text.startswith("$((", index)
                or char == "`"
            ):
                prefix = 1 if char == "`" else 2
                end = _substitution_end(text, index + prefix, char == "`")
                if substitutions is not None:
                    substitutions.append((text[index + prefix : end], line))
                consumed = text[index : end + 1]
                if "\n" in consumed:
                    line += consumed.count("\n")
                    column = len(consumed.rsplit("\n", 1)[-1])
                else:
                    column += len(consumed)
                index = end + 1
                word += "$()"
                continue
            if char == "\\" and quote != "'":
                if index + 1 == len(text):
                    raise SourceError(line, "dangling shell escape")
                following = text[index + 1]
                if following == "\n":
                    index, line, column = index + 2, line + 1, 0
                else:
                    word += following
                    index, column = index + 2, column + 2
                continue
            if char in "'\"" and not expansion_data:
                if not quote:
                    quote = char
                elif quote == char:
                    quote = ""
                else:
                    word += char
            else:
                word += char
            index += 1
            if char == "\n":
                line, column = line + 1, 0
            else:
                column += 1
        if quote:
            raise SourceError(start_line, "unterminated shell quote")
        tokens.append((word, start_line, start_column))
    return tokens


def _shell(scan: _Scan, text: str, offset: int = 0) -> None:
    lines = text.splitlines()
    masked = lines.copy()
    index = 0
    while index < len(lines):
        header = lines[index]
        match = re.search(r"<<(-?)[ \t]*(['\"]?)([A-Za-z_]\w*)\2", header)
        if not match:
            index += 1
            continue
        start = index + 1
        index = start
        while index < len(lines) and (
            lines[index].lstrip("\t") if match[1] else lines[index]
        ) != match[3]:
            index += 1
        if index == len(lines):
            raise SourceError(start, "unterminated shell heredoc")
        body = "\n".join(lines[start:index]) + "\n"
        words = [item[0] for item in _shell_tokens(header)]
        command = PurePosixPath(words[0]).name if words else ""
        language = "python" if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", command) else (
            "shell" if command in {"sh", "bash", "dash", "zsh"} else ""
        )
        if language:
            child = _Scan(scan.name, body, scan.policy)
            child.parse(language)
            for finding in validate_findings(scan.name, body, child.findings):
                scan.add(offset + start + finding.line, finding.kind, finding.column)
        if not match[2]:
            substitutions = []
            _shell_tokens(body, substitutions, expansion_data=True)
            for code, line in substitutions:
                _shell(scan, code, offset + start + line - 1)
        for pos in range(start, index + 1):
            masked[pos] = ""
        index += 1
    substitutions = []
    tokens = _shell_tokens("\n".join(masked), substitutions)
    for code, line in substitutions:
        _shell(scan, code, offset + line - 1)
    command = []
    variables: dict[str, str] = {}
    for token, line, column in [*tokens, ("\n", len(lines) + 1, 0)]:
        if token in {"\n", ";", ";;", "&&", "||", "|", "&", "(", ")"}:
            if command:
                words = [item[0] for item in command]
                for word in words:
                    if re.match(r"^[A-Za-z_]\w*=", word):
                        key, value = word.split("=", 1)
                        variables[key] = value
                words = [
                    variables.get(word[2:-1], word)
                    if word.startswith("${") and word.endswith("}")
                    else variables.get(word[1:], word) if re.fullmatch(r"\$\w+", word)
                    else word
                    for word in words
                ]
                _argv(scan, words, offset + command[0][1], command[0][2])
                command = []
        else:
            command.append((token, line, column))


_CMAKE_TOKEN = re.compile(
    r"(?P<bracket_comment>\#\[(?P<ceq>=*)\[.*?\](?P=ceq)\])|"
    r"(?P<comment>\#[^\n]*)|"
    r'(?P<quoted>"(?:\\.|[^"\\])*")|'
    r"(?P<bracket>\[(?P<beq>=*)\[.*?\](?P=beq)\])|"
    r"(?P<word>[^\s()\"#]+)|(?P<paren>[()])",
    re.S,
)


def _cmake(scan: _Scan) -> None:
    tokens = []
    for match in _CMAKE_TOKEN.finditer(scan.text):
        if match.group("bracket_comment") or match.group("comment"):
            continue
        value = match.group()
        if match.group("quoted"):
            value = value[1:-1]
        tokens.append(
            (value, scan.text.count("\n", 0, match.start()) + 1, match.start())
        )
    index = 0
    while index + 1 < len(tokens):
        command, _line, _column = tokens[index]
        if tokens[index + 1][0] != "(":
            index += 1
            continue
        index += 2
        depth, arguments = 1, []
        while index < len(tokens) and depth:
            token = tokens[index]
            depth += (token[0] == "(") - (token[0] == ")")
            if depth:
                arguments.append(token)
            index += 1
        if depth:
            raise SourceError(tokens[-1][1], "unterminated CMake command")
        command = command.lower()
        for position, (value, line, column) in enumerate(arguments):
            bounded = command in {
                "execute_process", "set_tests_properties", "set_property", "ctest_test",
            } and value in {
                "TIMEOUT", "TIMEOUT_AFTER_MATCH", "TIMEOUT_SIGNAL_GRACE_PERIOD",
            }
            bounded |= (
                command in {"set", "option"} and position == 0
                and value.upper() in {"CTEST_TEST_TIMEOUT", "DART_TESTING_TIMEOUT"}
            )
            if bounded:
                scan.add(line, "cmake-timeout", column)
        if command in {
            "execute_process", "add_test", "add_custom_command", "add_custom_target",
        }:
            for position, (value, line, column) in enumerate(arguments):
                if value.upper() == "COMMAND":
                    _argv(
                        scan, [arg[0] for arg in arguments[position + 1 :]],
                        line, column,
                    )


_NATIVE_DATA = re.compile(
    r"//[^\n]*|/\*.*?\*/|"
    r'R"(?P<delimiter>[A-Za-z0-9_]{0,16})\(.*?\)(?P=delimiter)"|'
    r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
    re.S,
)


def _native(scan: _Scan) -> None:
    code = _NATIVE_DATA.sub(
        lambda match: re.sub(r"[^\n]", " ", match.group()), scan.text
    )
    for match in re.finditer(
        r"\b(?:wait_for|wait_until|try_lock_for|try_lock_until|timed_join|"
        r"pthread_timedjoin_np|pthread_cond_timedwait|sem_timedwait)\s*\(", code
    ):
        scan.add(
            code.count("\n", 0, match.start()) + 1,
            "native-wait-timeout", match.start(),
        )
    for match in re.finditer(r"\b(?:alarm|ualarm|setitimer)\s*\(\s*([^,)]+)", code):
        if match[1].strip() != "0":
            scan.add(
                code.count("\n", 0, match.start()) + 1,
                "native-alarm", match.start(),
            )
    for match in re.finditer(
        r"\b(?:assert|require|CHECK|REQUIRE|ASSERT_[A-Z_]+)\s*\(", code
    ):
        start, end, depth = match.end(), match.end(), 1
        while end < len(code) and depth:
            depth += (code[end] == "(") - (code[end] == ")")
            end += 1
        condition = code[start : end - 1]
        if (
            re.search(
                r"\b(?:elapsed\w*|deadline\w*|duration\w*)\b|clock::now", condition
            )
            and re.search(r"[<>]|==|!=", condition)
        ):
            scan.add(
                code.count("\n", 0, match.start()) + 1,
                "native-elapsed-assertion", match.start(),
            )
    for match in re.finditer(r"\b(?:while|if)\s*\(([^{};]*?)\)\s*\{", code, re.S):
        if (
            re.search(r"(?:deadline|elapsed|clock::now)", match[1])
            and re.search(r"[<>]", match[1])
        ):
            scan.add(
                code.count("\n", 0, match.start()) + 1,
                "native-poll-deadline", match.start(),
            )


def _presets(scan: _Scan) -> None:
    data = json.loads(scan.text, object_pairs_hook=unique_object)
    if not isinstance(data, dict):
        raise SourceError(1, "CMake presets must be an object")
    for match in re.finditer(
        r'(?<!\\)"(?:timeout|CTEST_TEST_TIMEOUT|DART_TESTING_TIMEOUT)"\s*:', scan.text
    ):
        scan.add(
            scan.text.count("\n", 0, match.start()) + 1,
            "cmake-timeout", match.start(),
        )


def _config(scan: _Scan) -> None:
    if scan.name.endswith(".toml"):
        data = tomllib.loads(scan.text)
        sections = {
            "tool.pytest.ini_options":
                data.get("tool", {}).get("pytest", {}).get("ini_options", {})
        }
    else:
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.read_string(scan.text)
        sections = {section: dict(parser[section]) for section in parser.sections()}
    active = ""
    for number, line in enumerate(scan.lines, 1):
        section = re.match(r"\s*\[([^]]+)\]\s*(?:#.*)?$", line)
        if section:
            active = section[1]
        option = re.match(r"\s*([\w-]+)\s*=", line)
        if not option or active not in sections:
            continue
        key = option[1]
        value = sections[active].get(key, UNKNOWN)
        if active in {"pytest", "tool:pytest", "tool.pytest.ini_options"}:
            if key == "timeout" and str(value) not in {"0", "0.0", "None"}:
                scan.add(number, "pytest-timeout", option.start(1))
            if key == "addopts":
                words = value if isinstance(value, list) else shlex.split(str(value))
                child = _Scan(scan.name, line, scan.policy)
                _argv(child, ["pytest", *words], 1)
                for finding in child.findings:
                    scan.add(number, "pytest-timeout", finding.column)
        if active.startswith("testenv") and key in {
            "commands", "commands_pre", "commands_post",
        }:
            scan.embedded(str(value), "shell", number)

class _Python(_PythonBindings):
    visit_While = _PythonBindings.poll
    visit_For = _PythonBindings.poll
    visit_AsyncFor = _PythonBindings.poll

    def visit_Assert(self, node: ast.Assert) -> None:
        if self.clocky(node.test):
            self.add(node.test, "python-elapsed-assertion")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = self.ref(node.func)
        method = name.rsplit(".", 1)[-1]
        if isinstance(node.func, ast.Attribute) and method in {
            "write_text", "write_bytes",
        }:
            target = self.value(node.func.value)
            if not isinstance(target, _File) and isinstance(node.func.value, ast.Name):
                target = _File("symbol:" + node.func.value.id)
                self.bind(node.func.value, target)
            if isinstance(target, _File):
                code = self.value(node.args[0]) if node.args else next(
                    (
                        self.value(item.value)
                        for item in node.keywords if item.arg == "data"
                    ),
                    UNKNOWN,
                )
                if isinstance(code, bytes):
                    try:
                        code = code.decode("utf-8")
                    except UnicodeError:
                        code = UNKNOWN
                self.generated[target.name] = code
        diagnostic = method in {
            "subTest", "assertEqual", "assertNotEqual", "assertIs", "assertIsNot",
            "assertTrue", "assertFalse", "assertIsNone", "assertIsNotNone",
            "assertIn", "assertNotIn", "assertIsInstance", "assertNotIsInstance",
            "assertLess", "assertLessEqual", "assertGreater", "assertGreaterEqual",
            "assertAlmostEqual", "assertNotAlmostEqual", "assertCountEqual",
            "assertDictEqual", "assertListEqual", "assertTupleEqual",
            "assertSequenceEqual", "assertSetEqual", "assertMultiLineEqual",
            "assertRegex", "assertNotRegex", "assertRaises", "assertRaisesRegex",
            "assertWarns", "assertWarnsRegex", "assertLogs", "assertNoLogs",
            "assert_called_with", "assert_called_once_with", "assert_any_call",
            "assert_has_calls",
        }
        data_constructor = (
            name == "dict" and self.lookup("dict") is UNKNOWN
        ) or name in {
            "collections.OrderedDict", "collections.defaultdict", "types.SimpleNamespace",
        }
        if (
            (method.startswith("assert") or method in {"require", "check"})
            and any(self.clocky(arg) for arg in node.args)
        ):
            self.add(node, "python-elapsed-assertion")
        data_update = (
            isinstance(node.func, ast.Attribute) and method == "update"
            and isinstance(self.value(node.func.value), dict)
        )
        if data_update:
            container = self.value(node.func.value)
            if isinstance(container, dict):
                for argument in node.args:
                    more = self.value(argument)
                    if isinstance(more, dict):
                        container.update(more)
                    else:
                        container["timeout"] = UNKNOWN
                for keyword in node.keywords:
                    if keyword.arg is not None:
                        container[keyword.arg] = self.value(keyword.value)
        if isinstance(node.func, ast.Attribute) and method in {"append", "extend"}:
            container = self.value(node.func.value)
            if isinstance(container, list) and node.args:
                value = self.value(node.args[0])
                if method == "append":
                    container.append(value)
                else:
                    container.extend(value if isinstance(value, list) else [UNKNOWN])

        kind = ""
        position = None
        zero_disables = False
        subprocess_call = name in {
            "subprocess.run", "subprocess.call", "subprocess.check_call",
            "subprocess.check_output", "subprocess.Popen",
        }
        if subprocess_call:
            kind = "python-subprocess-timeout"
        elif name == "select.select":
            kind, position = "python-wait-timeout", 3
        elif method in {"settimeout", "setdefaulttimeout"}:
            kind, position = "python-wait-timeout", 0
        elif name in {
            "asyncio.wait_for", "asyncio.wait", "asyncio.timeout", "asyncio.timeout_at",
        }:
            kind = "python-async-timeout"
            position = 1 if name == "asyncio.wait_for" else (
                0 if name in {"asyncio.timeout", "asyncio.timeout_at"} else None
            )
        elif name in {"signal.alarm", "signal.setitimer"}:
            kind, position, zero_disables = "python-alarm", (
                0 if name == "signal.alarm" else 1
            ), True
        elif name == "pytest.mark.timeout":
            kind, position, zero_disables = "pytest-timeout", 0, True
        elif method in {
            "wait", "wait_for", "join", "communicate", "get", "acquire",
            "result", "exception",
        }:
            receiver = (
                self.value(node.func.value)
                if isinstance(node.func, ast.Attribute) else UNKNOWN
            )
            # str.join and dict.get are data operations, not bounded waits.
            if not (
                (
                    method == "join"
                    and (isinstance(receiver, (str, list)) or name == "str.join")
                )
                or (
                    method == "get"
                    and (isinstance(receiver, dict) or name == "dict.get")
                )
            ):
                kind = "python-wait-timeout"
                if method in {"wait", "join", "result", "exception"}:
                    position = 0
                elif method in {"communicate", "wait_for"}:
                    position = 1
                elif method == "acquire" or "Queue()" in name:
                    position = 1

        options = []
        for keyword in node.keywords:
            if keyword.arg is not None:
                options.append((keyword.arg, self.value(keyword.value), keyword))
            else:
                expanded = self.value(keyword.value)
                if isinstance(expanded, dict):
                    options.extend(
                        (key, value, keyword) for key, value in expanded.items()
                    )
                elif kind and not diagnostic:
                    self.add(keyword, "python-dynamic-options")
        if not diagnostic and not data_constructor and not data_update:
            for key, value, origin in options:
                special_key = (
                    (name == "asyncio.timeout" and key == "delay")
                    or (name == "asyncio.timeout_at" and key == "when")
                    or (
                        name in {"signal.alarm", "signal.setitimer"}
                        and key == "seconds"
                    )
                )
                if (
                    (TIME_OPTION.search(key) or special_key) and value is not None
                    and not (zero_disables and value == 0)
                ):
                    self.add(origin, kind or "python-timeout-option")
            if position is not None and len(node.args) > position:
                argument = node.args[position]
                value = self.value(argument)
                if value is not None and not (zero_disables and value == 0):
                    self.add(argument, kind)
        if subprocess_call:
            command = self.value(node.args[0]) if node.args else next(
                (value for key, value, _origin in options if key == "args"), UNKNOWN
            )
            if isinstance(command, str):
                self.scan.embedded(command, "shell", node.lineno)
            elif isinstance(command, list):
                _argv(self.scan, command, node.lineno, node.col_offset)
            self.generated_command(command, node)
        elif method in self.executors and not diagnostic:
            words = []
            for argument in node.args:
                value = self.value(
                    argument.value if isinstance(argument, ast.Starred) else argument
                )
                words.extend(value if isinstance(value, list) else [value])
            for _key, value, _origin in options:
                if isinstance(value, list):
                    words.extend(value)
            _argv(self.scan, [UNKNOWN, *words], node.lineno, node.col_offset)
        if name in {"os.system", "os.popen"} and node.args:
            command = self.value(node.args[0])
            if isinstance(command, str):
                self.scan.embedded(command, "shell", node.lineno)
            else:
                self.add(node, "python-dynamic-code")
        if name in {"exec", "eval", "builtins.exec", "builtins.eval"} and node.args:
            code = self.value(node.args[0])
            if isinstance(code, str):
                self.scan.embedded(code, "python", node.lineno)
            else:
                self.add(node, "python-dynamic-code")
        self.generic_visit(node)

    def generated_command(self, command: object, node: ast.Call) -> None:
        words = command if isinstance(command, list) else [command]
        if not words:
            return
        executable = words[0]
        program = PurePosixPath(executable).name if isinstance(executable, str) else ""
        interpreter = re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", program) or program in {
            "sh", "bash", "dash", "zsh", "cmake", "cc", "c++",
            "gcc", "g++", "clang", "clang++",
        }
        for index, word in enumerate(words):
            if not isinstance(word, _File) or (index != 0 and not interpreter):
                continue
            if word.name not in self.generated:
                continue
            code = self.generated[word.name]
            if not isinstance(code, str):
                self.add(node, "python-dynamic-code")
                continue
            language = source_kind(word.name)
            if language is None and code.startswith("#!"):
                language = "python" if "python" in code.splitlines()[0] else "shell"
            if language not in {"python", "shell", "cmake", "native"}:
                self.add(node, "python-dynamic-code")
            else:
                self.scan.embedded(code, language, node.lineno)


def _ymap(node: YNode) -> dict[str, YNode]:
    return node.value if isinstance(node.value, dict) else {}


def _yscalar(node: YNode | None) -> str:
    return node.value if node is not None and isinstance(node.value, str) else ""


def _cd_eligible(job: YNode, job_id: str, purpose: str) -> bool:
    """Conservative CD classification from executable steps, never a job name.

    Only pinned known setup/deployment actions and direct CD commands qualify.
    Opaque scripts, reusable jobs, test commands and unknown actions cannot
    inherit a grant. Adding another CD mechanism requires a reviewed parser
    change as well as a measured exact policy entry.
    """
    fields = _ymap(job)
    if not fields or "uses" in fields or "steps" not in fields:
        return False
    label = job_id + " " + _yscalar(fields.get("name"))
    if re.search(
        r"(?:^|[\W_])(?:tests?|checks?|lint|benchmark|build)(?:$|[\W_])",
        label, re.I,
    ):
        return False
    environment = fields.get("environment")
    if environment is None:
        return False
    if not (_yscalar(environment) or _yscalar(_ymap(environment).get("name"))):
        return False
    steps = fields["steps"].value
    if not isinstance(steps, list) or not steps:
        return False
    setup_actions = {
        "actions/checkout", "actions/setup-python", "actions/setup-node",
        "actions/upload-artifact", "actions/download-artifact",
    }
    cd_actions = {
        "actions/deploy-pages": "deploy",
        "aws-actions/amazon-ecs-deploy-task-definition": "deploy",
    }
    effects = set()
    for step in steps:
        values = _ymap(step)
        if not values or ("run" in values) == ("uses" in values):
            return False
        if "uses" in values:
            action = _yscalar(values["uses"])
            if not re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", action):
                return False
            action = action.split("@", 1)[0]
            if action in cd_actions:
                effects.add(cd_actions[action])
            elif action not in setup_actions:
                return False
        else:
            script = _yscalar(values["run"])
            if not script:
                return False
            for line in script.splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                try:
                    words = shlex.split(line, comments=True)
                except ValueError:
                    return False
                # Reject shell control flow, substitutions, hooks and wrappers.
                if re.search(r"[;&|`<>]|\$\(", line):
                    return False
                prefixes = {
                    ("wrangler", "deploy"): "deploy",
                    ("kubectl", "apply"): "deploy",
                    ("kubectl", "rollout", "undo"): "rollback",
                    ("gh", "release", "create"): "release",
                    ("gh", "release", "upload"): "release",
                    ("alembic", "upgrade"): "migrate",
                    ("prisma", "migrate", "deploy"): "migrate",
                }
                effect = next(
                    (kind for prefix, kind in prefixes.items()
                     if tuple(words[: len(prefix)]) == prefix), ""
                )
                if not effect:
                    return False
                effects.add(effect)
    return effects == {purpose}


def _workflow(scan: _Scan, document: YNode) -> None:
    root = _ymap(document)
    if not root:
        raise SourceError(document.line, "YAML root must be a mapping")
    jobs = root.get("jobs")
    is_workflow = scan.name.startswith(".github/workflows/")
    if is_workflow and (
        jobs is None or not isinstance(jobs.value, dict) or not jobs.value
    ):
        raise SourceError(document.line, "workflow requires a nonempty jobs mapping")

    def default_shell(path: tuple) -> str:
        parents = []
        if len(path) >= 2 and path[0] == "jobs" and jobs is not None:
            parents.append(_ymap(jobs).get(path[1], YNode({}, 1)))
        parents.append(document)
        for parent in parents:
            defaults = _ymap(parent).get("defaults", YNode({}, 1))
            run = _ymap(defaults).get("run", YNode({}, 1))
            shell = _yscalar(_ymap(run).get("shell"))
            if shell:
                return shell
        return "bash"

    def walk(node: YNode, path: tuple = ()) -> None:
        if isinstance(node.value, list):
            for index, item in enumerate(node.value):
                walk(item, (*path, index))
        elif isinstance(node.value, dict):
            for key, value in node.value.items():
                target = (*path, key)
                line, column = node.marks[key]
                if key == "timeout-minutes":
                    if len(target) == 3 and target[0] == "jobs":
                        entry = next(
                            (item for item in scan.policy.entries
                             if (item.workflow, item.job) == (scan.name, target[1])),
                            None,
                        )
                        bound = _yscalar(value)
                        if (
                            entry is not None
                            and re.fullmatch(r"[1-9][0-9]*", bound)
                            and int(bound) == entry.timeout_minutes
                            and _cd_eligible(node, target[1], entry.purpose)
                        ):
                            scan.allowed.append((scan.name, target[1]))
                        else:
                            scan.add(line, "workflow-job-timeout", column)
                    elif (
                        len(target) == 5 and target[0] == "jobs"
                        and target[2] == "steps" and isinstance(target[3], int)
                    ):
                        scan.add(line, "workflow-step-timeout", column)
                    else:
                        scan.add(line, "workflow-timeout", column)
                elif TIME_OPTION.search(key.lower().replace("-", "_")):
                    scan.add(line, "workflow-timeout", column)
                if key == "run" and isinstance(value.value, str):
                    shell = _yscalar(node.value.get("shell")) or default_shell(path)
                    language = (
                        "python" if re.match(r"python(?:\d|\s|$)", shell)
                        else "shell" if re.match(r"(?:bash|sh|dash|zsh)(?:\s|$)", shell)
                        else ""
                    )
                    if not language:
                        scan.add(line, "unsupported-source", column)
                    else:
                        child = _Scan(scan.name, value.value, scan.policy)
                        child.parse(language)
                        for finding in validate_findings(
                            scan.name, value.value, child.findings
                        ):
                            scan.add(
                                value.line + finding.line - 1,
                                finding.kind, finding.column,
                            )
                elif key == "script":
                    # A JS/PowerShell action body cannot be pronounced clean by
                    # feeding it to the Python or shell scanner.
                    scan.add(line, "unsupported-source", column)
                walk(value, target)

    walk(document)


def source_kind(name: str, mode: str = "100644") -> str | None:
    """Select first-party tracked code/config without test/helper name filters."""
    relative_name(name)
    if name.split("/", 1)[0] in {"external", "third_party"}:
        return None
    path = PurePosixPath(name)
    if name == POLICY_PATH:
        return "policy"
    if path.suffix in {".py", ".pyi", ".pyw"}:
        return "python"
    if path.suffix in {".sh", ".bash", ".zsh"}:
        return "shell"
    if path.name == "CMakeLists.txt" or path.suffix == ".cmake":
        return "cmake"
    if path.suffix in {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}:
        return "native"
    if path.suffix in {".yml", ".yaml"}:
        return "yaml"
    if path.suffix in {".toml", ".ini", ".cfg"}:
        return "config"
    if path.name in {"CMakePresets.json", "CMakeUserPresets.json"}:
        return "presets"
    if mode == "100755" or path.suffix in {
        ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".ps1", ".bat", ".cmd",
        ".rb", ".pl", ".go", ".rs",
    }:
        return "unsupported"
    return None


def select_population(records: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Validate inventory and select every eligible source, including this guard."""
    if not isinstance(records, list) or not records:
        raise ValueError("empty or missing tracked population")
    seen = set()
    selected = []
    for mode, name in records:
        relative_name(name)
        if name in seen:
            raise ValueError("duplicate tracked path")
        seen.add(name)
        if mode not in {"100644", "100755", "120000", "160000"}:
            raise ValueError("malformed tracked mode")
        kind = source_kind(name, mode)
        if kind is not None:
            if mode not in {"100644", "100755"}:
                raise ValueError(f"source must be a regular tracked file: {name}")
            selected.append((name, kind))
    required = {
        "CMakeLists.txt", "pyproject.toml", POLICY_PATH,
        "tests/test_no_time_limits.py", "tests/_no_time_limits.py",
    }
    if not required <= seen or not selected:
        raise ValueError("incomplete tracked code population")
    for prefix in ("tests/", "scripts/", ".github/workflows/"):
        if not any(name.startswith(prefix) for name, _kind in selected):
            raise ValueError(f"missing tracked code population: {prefix}")
    return sorted(selected)


def tracked_population(root: Path) -> list[tuple[str, str]]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--stage", "-z"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )
    records = []
    if not result.stdout.endswith(b"\0"):
        raise ValueError("incomplete tracked population output")
    for record in result.stdout.split(b"\0")[:-1]:
        metadata, separator, path = record.partition(b"\t")
        fields = metadata.split()
        if (
            not separator or len(fields) != 3 or fields[2] != b"0"
            or not re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})", fields[1])
        ):
            raise ValueError("malformed or unmerged tracked population")
        records.append((fields[0].decode("ascii"), path.decode("utf-8")))
    return select_population(records)


def read_source(root: Path, name: str) -> str:
    relative_name(name)
    path = root / name
    if path.is_symlink() or not path.resolve(strict=True).is_relative_to(
        root.resolve()
    ):
        raise ValueError(f"source escapes the worktree: {name}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"empty source cannot establish coverage: {name}")
    return text


def _scan_source(
    name: str, text: str, policy: Policy | None, language: str | None = None
) -> _Scan:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty or malformed source")
    scan = _Scan(name, text, policy)
    kind = language or source_kind(name)
    if kind == "policy":
        parse_policy(text)
    else:
        scan.parse(kind or "unsupported")
    scan.findings = validate_findings(name, text, scan.findings)
    return scan


def scan_limits(name: str, text: str, policy: Policy | None = None) -> list[Finding]:
    """The single implementation used by both controls and the repository check."""
    return _scan_source(name, text, policy).findings


def scan_repository(root: Path) -> tuple[list[tuple[str, str]], list[Finding]]:
    population = tracked_population(root)
    policy = parse_policy(read_source(root, POLICY_PATH))
    findings = []
    allowed = []
    for name, language in population:
        text = read_source(root, name)
        scan = _scan_source(name, text, policy, language)
        findings.extend(scan.findings)
        allowed.extend(scan.allowed)
    expected = [(entry.workflow, entry.job) for entry in policy.entries]
    if sorted(allowed) != sorted(expected):
        raise ValueError("stale, ineligible or multiply consumed CD policy entry")
    return population, findings
