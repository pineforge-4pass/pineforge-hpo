#!/usr/bin/env python3
"""Expose only module exports and public class members to Doxygen."""

from __future__ import annotations

import ast
from pathlib import Path
import sys
from typing import Iterable


_DEFINITIONS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _is_private(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def _assigned_names(node: ast.Assign | ast.AnnAssign) -> Iterable[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    for target in targets:
        if isinstance(target, ast.Name):
            yield target.id
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                if isinstance(element, ast.Name):
                    yield element.id


def _start_line(node: ast.AST) -> int:
    decorators = getattr(node, "decorator_list", ())
    return min([node.lineno, *(decorator.lineno for decorator in decorators)]) - 1


def _module_exports(tree: ast.Module) -> frozenset[str] | None:
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        names = tuple(_assigned_names(node))
        if "__all__" not in names:
            continue
        try:
            values = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            return None
        if isinstance(values, (list, tuple)) and all(
            isinstance(value, str) for value in values
        ):
            return frozenset(values)
        return None
    return None


def _hidden_ranges(
    body: list[ast.stmt], *, exports: frozenset[str] | None = None
) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for node in body:
        if isinstance(node, _DEFINITIONS):
            if _is_private(node.name) or (
                exports is not None and node.name not in exports
            ):
                ranges.append((_start_line(node), node.end_lineno or node.lineno))
            elif isinstance(node, ast.ClassDef):
                ranges.extend(_hidden_ranges(node.body))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            names = tuple(_assigned_names(node))
            if names and (
                any(_is_private(name) for name in names)
                or (exports is not None and not any(name in exports for name in names))
            ):
                ranges.append((node.lineno - 1, node.end_lineno or node.lineno))
    return ranges


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: filter_public_python.py PATH")

    path = Path(sys.argv[1])
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    tree = ast.parse(source, filename=str(path))

    for start, end in _hidden_ranges(tree.body, exports=_module_exports(tree)):
        for index in range(start, end):
            lines[index] = "\n" if lines[index].endswith("\n") else ""

    sys.stdout.write("".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
