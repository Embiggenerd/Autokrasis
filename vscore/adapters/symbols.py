"""Top-level Python symbols and their line spans, for function-level scores and prices."""

from __future__ import annotations

import ast
import warnings
from dataclasses import dataclass


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str  # function | class | variable
    start: int  # first line, decorators included
    end: int


def python_symbols(source: bytes | str) -> list[Symbol] | None:
    """Functions, classes and assigned names at module level; None if the source does not parse."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    symbols = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = min([d.lineno for d in node.decorator_list] + [node.lineno])
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            symbols.append(Symbol(node.name, kind, start, node.end_lineno or node.lineno))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    symbols.append(Symbol(target.id, "variable", node.lineno, node.end_lineno or node.lineno))
    return symbols


def removed_symbols(old: str, new: str) -> list[Symbol]:
    """Top-level symbols in `old` that `new` no longer defines; nothing if either side does not parse."""
    before, after = python_symbols(old), python_symbols(new)
    if before is None or after is None:
        return []
    kept = {s.name for s in after}
    return [s for s in before if s.name not in kept]
