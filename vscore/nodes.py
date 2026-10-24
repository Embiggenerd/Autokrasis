"""Node identities and their continuity across renames (plan: Value follows code)."""

from __future__ import annotations

from vscore.gitrepo import Repo, decode_path


def file_node(path: str) -> str:
    return f"file:{path}"


def symbol_node(path: str, name: str) -> str:
    return f"sym:{path}::{name}"


def table_node(database: str, schema: str, name: str) -> str:
    return f"table:{database}.{schema}.{name}"


def node_path(node: str) -> str | None:
    """The file path a file or symbol node lives in; None for tables."""
    if node.startswith("file:"):
        return node[5:]
    if node.startswith("sym:"):
        return node[4:].split("::", 1)[0]
    return None


class Lineage:
    """Maps any historical path to the path the same file has at a revision, following renames.

    Built newest to oldest from `git log -M`: when a commit renames `old` to `new` and `new` maps to a
    file at the revision, `old` maps there too. Paths that were deleted before the revision map to None.
    """

    def __init__(self, current: set[str], renames: list[tuple[str, str]]):
        self._alias = {p: p for p in current}
        for old, new in renames:
            if new in self._alias and old not in self._alias:
                self._alias[old] = self._alias[new]

    @classmethod
    def from_git(cls, repo: Repo, rev: str, current: set[str]) -> Lineage:
        output = repo.run("log", "-M", "--diff-filter=R", "--name-status", "-z", "--format=", rev)
        tokens = [t for t in output.split(b"\0")]
        renames = []
        i = 0
        while i < len(tokens):
            token = tokens[i].strip(b"\n")
            if token.startswith(b"R") and i + 2 < len(tokens):
                renames.append((decode_path(tokens[i + 1]), decode_path(tokens[i + 2])))
                i += 3
            else:
                i += 1
        return cls(current, renames)

    def current(self, path: str) -> str | None:
        return self._alias.get(path)
