"""Python import graph and dependents D(f) (scoring spec v1: Import graph)."""

from __future__ import annotations

import ast
import posixpath
import warnings
from dataclasses import dataclass, field

from vscore.adapters.jsimports import JS_EXTENSIONS, JsResolver, extract_js

_DYNAMIC_IMPORTERS = {"import_module", "__import__"}
_REEXPORT_HOPS = 10

# One import request: (level, module parts, imported name). `import a.b` is (0, ("a", "b"), None);
# `from .a import b` is (1, ("a",), "b"); `from a import *` is (0, ("a",), None).
Request = tuple[int, tuple[str, ...], str | None]


@dataclass
class ImportCoverage:
    imports: int = 0
    resolved: int = 0
    unresolved: int = 0
    dynamic: int = 0
    parse_errors: int = 0
    via_reexport: int = 0
    type_only: int = 0


@dataclass
class ImportGraph:
    edges: dict[str, set[str]]
    parse_errors: set[str] = field(default_factory=set)
    coverage: ImportCoverage = field(default_factory=ImportCoverage)
    names: dict[str, dict[str, set[str]]] = field(default_factory=dict)  # file -> imported name -> importers

    def name_dependents(self, path: str, name: str, not_counted: set[str]) -> int:
        """Files outside `not_counted` that import `name` from `path` by name."""
        return len(self.names.get(path, {}).get(name, set()) - not_counted)


@dataclass
class Extracted:
    requests: list[Request]
    dynamic: int
    bound: dict[str, Request]
    type_only: int


def _is_type_checking(test: ast.expr) -> bool:
    """`if TYPE_CHECKING:` or `if typing.TYPE_CHECKING:` (any module alias)."""
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _runtime_nodes(tree: ast.AST):
    """Every node that can run, skipping the bodies of `if TYPE_CHECKING:` blocks (their `else` still runs)."""
    stack = [tree]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            stack.extend(reversed(node.orelse))
        else:
            stack.extend(reversed(list(ast.iter_child_nodes(node))))


def extract(source: bytes, path: str) -> Extracted:
    """Runtime import requests, dynamic imports, type-only imports, and the names module-level imports bind.

    The bound names are what a package `__init__.py` re-exports: `from .app import Flask` binds
    "Flask" to the request (1, ("app",), "Flask"). Raises SyntaxError or ValueError.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tree = ast.parse(source, filename=path)
    requests: list[Request] = []
    dynamic = 0
    bound: dict[str, Request] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            parts = tuple(node.module.split(".")) if node.module else ()
            for alias in node.names:
                if alias.name != "*":
                    bound[alias.asname or alias.name] = (node.level, parts, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bound[alias.asname] = (0, tuple(alias.name.split(".")), None)
    for node in _runtime_nodes(tree):
        if isinstance(node, ast.Import):
            requests.extend((0, tuple(alias.name.split(".")), None) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            parts = tuple(node.module.split(".")) if node.module else ()
            requests.extend((node.level, parts, None if a.name == "*" else a.name) for a in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
            if name in _DYNAMIC_IMPORTERS:
                dynamic += 1
    every_import = sum(len(n.names) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)))
    return Extracted(requests, dynamic, bound, every_import - len(requests))


def _join(*parts: str) -> str:
    return "/".join(p for p in parts if p)


class Resolver:
    """Maps import requests to repository files, following the spec's resolution rules."""

    def __init__(self, paths: set[str], roots: tuple[str, ...]):
        self.paths = paths
        self.roots = roots

    def _module_file(self, base: str, parts: tuple[str, ...]) -> str | None:
        if parts:
            stem = _join(base, *parts)
            candidates = (stem + ".py", stem + "/__init__.py")
        else:
            candidates = (_join(base, "__init__.py"),)
        return next((c for c in candidates if c in self.paths), None)

    def resolve(self, importer: str, request: Request) -> tuple[str | None, str | None]:
        """The file a request resolves to, and the name it imports from inside that file (None when
        the request imports a whole module)."""
        level, parts, name = request
        if level == 0:
            bases, min_len = self.roots, 1
        else:
            base = posixpath.dirname(importer)
            for _ in range(level - 1):
                if not base:
                    return None, None
                base = posixpath.dirname(base)
            bases, min_len = (base,), 0
        if name is not None:
            for base in bases:
                if hit := self._module_file(base, parts + (name,)):
                    return hit, None
        for k in range(len(parts), min_len - 1, -1):
            for base in bases:
                if hit := self._module_file(base, parts[:k]):
                    return hit, name if k == len(parts) else None
        return None, None

    def follow(self, target: str, name: str | None, bound: dict[str, dict[str, Request]]) -> tuple[str, str | None]:
        """Traces a name imported from a package to the file its __init__.py re-exports it from.

        Follows chains of __init__.py files for up to _REEXPORT_HOPS hops. A name the __init__.py
        defines itself, or binds through a star import, stays with the __init__.py.
        """
        seen = set()
        while name is not None and target.endswith("__init__.py") and target not in seen:
            if len(seen) == _REEXPORT_HOPS:
                break
            seen.add(target)
            request = bound.get(target, {}).get(name)
            if request is None:
                break
            hit, inner = self.resolve(target, request)
            if hit is None:
                break
            target, name = hit, inner
        return target, name


def is_code(path: str) -> bool:
    """Files whose imports the scorer reads: Python, JavaScript and TypeScript."""
    return path.endswith(".py") or path.endswith(JS_EXTENSIONS)


def build_graph(sources: dict[str, bytes], roots: tuple[str, ...]) -> ImportGraph:
    """Edges run from the importing file to the imported file; only files in `sources` can be targets."""
    graph = ImportGraph(edges={path: set() for path in sources})
    _python_edges(graph, {p: s for p, s in sources.items() if p.endswith(".py")}, roots)
    _js_edges(graph, {p: s for p, s in sources.items() if p.endswith(JS_EXTENSIONS)})
    return graph


def _js_edges(graph: ImportGraph, sources: dict[str, bytes]) -> None:
    resolver = JsResolver(set(sources))
    coverage = graph.coverage
    parsed = {}
    for path in sorted(sources):
        try:
            parsed[path] = extract_js(sources[path].decode("utf-8"))
        except UnicodeDecodeError:
            graph.parse_errors.add(path)
            coverage.parse_errors += 1
    bound = {p: found.bound for p, found in parsed.items() if posixpath.basename(p).startswith("index.")}
    for path, found in parsed.items():
        coverage.imports += len(found.requests) + found.dynamic
        coverage.dynamic += found.dynamic
        coverage.unresolved += found.dynamic
        coverage.type_only += found.type_only
        for spec, name in found.requests:
            target = resolver.resolve(path, spec)
            if target is None:
                coverage.unresolved += 1
                continue
            coverage.resolved += 1
            followed, name = resolver.follow(target, name, bound)
            if followed != target:
                coverage.via_reexport += 1
                target = followed
            if target != path:
                graph.edges[path].add(target)
                if name is not None:
                    graph.names.setdefault(target, {}).setdefault(name, set()).add(path)


def _python_edges(graph: ImportGraph, sources: dict[str, bytes], roots: tuple[str, ...]) -> None:
    resolver = Resolver(set(sources), roots)
    coverage = graph.coverage
    parsed = {}
    for path in sorted(sources):
        try:
            parsed[path] = extract(sources[path], path)
        except (SyntaxError, ValueError, RecursionError):
            graph.parse_errors.add(path)
            coverage.parse_errors += 1
    bound = {path: found.bound for path, found in parsed.items() if path.endswith("__init__.py")}

    for path, found in parsed.items():
        coverage.imports += len(found.requests) + found.dynamic
        coverage.dynamic += found.dynamic
        coverage.unresolved += found.dynamic
        coverage.type_only += found.type_only
        for request in found.requests:
            target, name = resolver.resolve(path, request)
            if target is None:
                coverage.unresolved += 1
                continue
            coverage.resolved += 1
            followed, name = resolver.follow(target, name, bound)
            if followed != target:
                coverage.via_reexport += 1
                target = followed
            if target != path:
                graph.edges[path].add(target)
                if name is not None:
                    graph.names.setdefault(target, {}).setdefault(name, set()).add(path)


def dependents(graph: ImportGraph, not_counted: set[str]) -> dict[str, int | None]:
    """D(f): distinct files with a direct import edge to f, excluding f and every file in `not_counted`.

    Direct rather than transitive, because modules that import each other in a cycle would all
    reach each other and get the same D. `not_counted` holds the test-class files. A file that
    failed to parse gets None.
    """
    importers: dict[str, set[str]] = {path: set() for path in graph.edges}
    for source, targets in graph.edges.items():
        if source in not_counted:
            continue
        for target in targets:
            importers[target].add(source)
    return {path: None if path in graph.parse_errors else len(importers[path]) for path in graph.edges}
