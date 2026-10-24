"""JavaScript and TypeScript imports, with the same rules as Python.

Relative specifiers ("./x", "../y") resolve to files; bare specifiers (packages) and path aliases stay
unresolved. `import type` and `export type` never run, so they add no edge. A named import that lands
on an `index` file follows that file's `export { name } from "./x"` to the defining file.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

JS_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")

# A JS request: (specifier, imported name). The name is None for side-effect, namespace and require imports.
JsRequest = tuple[str, str | None]

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"(?m)^\s*//.*$")
_IMPORT = re.compile(r"""\bimport\s+(type\s+)?([\w$*{}\s,]+?)\s+from\s+['"]([^'"]+)['"]""")
_SIDE_EFFECT = re.compile(r"""\bimport\s+['"]([^'"]+)['"]""")
_EXPORT_FROM = re.compile(r"""\bexport\s+(type\s+)?(\*(?:\s+as\s+[\w$]+)?|\{[^}]*\})\s+from\s+['"]([^'"]+)['"]""")
_REQUIRE = re.compile(r"""\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)""")
_DYNAMIC_LITERAL = re.compile(r"""\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)""")
_DYNAMIC_ANY = re.compile(r"""\bimport\s*\(""")


@dataclass
class JsExtracted:
    requests: list[JsRequest] = field(default_factory=list)
    dynamic: int = 0
    bound: dict[str, JsRequest] = field(default_factory=dict)  # names an index file re-exports
    type_only: int = 0


def _names(clause: str) -> list[str | None]:
    """Imported names from an import clause: default -> "default", `{ a, b as c }` -> a, b, `* as x` -> None."""
    clause = clause.strip()
    names: list[str | None] = []
    braces = re.search(r"\{([^}]*)\}", clause)
    head = clause[: braces.start()] if braces else clause
    for part in (p.strip() for p in head.split(",")):
        if part.startswith("*"):
            names.append(None)
        elif part:
            names.append("default")
    if braces:
        for item in (i.strip() for i in braces.group(1).split(",")):
            if item and not item.startswith("type "):
                names.append(item.split(" as ")[0].strip())
    return names or [None]


def extract_js(source: str) -> JsExtracted:
    text = _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", source))
    found = JsExtracted()
    for type_only, clause, spec in _IMPORT.findall(text):
        if type_only:
            found.type_only += 1
            continue
        found.requests.extend((spec, name) for name in _names(clause))
    found.requests.extend((spec, None) for spec in _SIDE_EFFECT.findall(text))
    for type_only, clause, spec in _EXPORT_FROM.findall(text):
        if type_only:
            found.type_only += 1
            continue
        if clause.startswith("{"):
            for item in (i.strip() for i in clause[1:-1].split(",")):
                if item and not item.startswith("type "):
                    original, _, alias = item.partition(" as ")
                    found.bound[(alias or original).strip()] = (spec, original.strip())
                    found.requests.append((spec, original.strip()))
        else:
            found.requests.append((spec, None))
    found.requests.extend((spec, None) for spec in _REQUIRE.findall(text))
    literal = _DYNAMIC_LITERAL.findall(text)
    found.requests.extend((spec, None) for spec in literal)
    found.dynamic = len(_DYNAMIC_ANY.findall(text)) - len(literal)
    return found


class JsResolver:
    def __init__(self, paths: set[str]):
        self.paths = paths

    def resolve(self, importer: str, spec: str) -> str | None:
        if not spec.startswith("."):
            return None
        stem = posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))
        if stem.startswith(".."):
            return None
        candidates = [stem] if stem.endswith(JS_EXTENSIONS) else []
        if stem.endswith((".js", ".jsx", ".mjs", ".cjs")):  # TypeScript imports "./x.js" meaning x.ts
            base = stem.rsplit(".", 1)[0]
            candidates += [base + ext for ext in (".ts", ".tsx", ".mts", ".cts")]
        candidates += [stem + ext for ext in JS_EXTENSIONS]
        candidates += [f"{stem}/index{ext}" for ext in JS_EXTENSIONS]
        return next((c for c in candidates if c in self.paths), None)

    def follow(self, target: str, name: str | None, bound: dict[str, dict[str, JsRequest]],
               hops: int = 10) -> tuple[str, str | None]:
        """Traces a named import that landed on an index file to the file that index re-exports it from."""
        seen = set()
        while name is not None and posixpath.basename(target).startswith("index.") and target not in seen:
            if len(seen) == hops:
                break
            seen.add(target)
            request = bound.get(target, {}).get(name)
            if request is None:
                break
            hit = self.resolve(target, request[0])
            if hit is None:
                break
            target, name = hit, request[1]
        return target, name
