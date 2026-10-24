"""Which tracked files are eligible, and each file's class (scoring spec v1: Eligible files, Classification precedence)."""

from __future__ import annotations

import fnmatch
import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass

from vscore.config import Config
from vscore.gitrepo import TreeEntry

CROWN_JEWEL = "crown_jewel"
SCHEMA = "schema"
GENERATED = "generated"
SENSITIVE = "sensitive"
TEST = "test"
CODE = "code"

GITATTRIBUTES = ("linguist-generated", "linguist-vendored")

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class FileRecord:
    path: str
    sha: str
    size: int
    cls: str


def parse_crown_jewels(text: str) -> tuple[str, ...]:
    """Reads crown_jewels.yml: a YAML list of paths or glob patterns, one `- item` per line."""
    patterns = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if not line.startswith("- "):
            raise ValueError(f"crown_jewels.yml: expected '- <path or glob>', got {raw!r}")
        item = line[2:].strip().strip("'\"")
        if item:
            patterns.append(item)
    return tuple(patterns)


def _attribute_set(value: str | None) -> bool:
    return value in ("set", "true")


def _dirs(path: str) -> list[str]:
    return path.split("/")[:-1]


def _contains_run(segments: list[str], run: list[str]) -> bool:
    return any(segments[i : i + len(run)] == run for i in range(len(segments) - len(run) + 1))


def is_crown_jewel(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


def is_schema(path: str) -> bool:
    dirs = _dirs(path)
    return "migrations" in dirs or _contains_run(dirs, ["alembic", "versions"]) or path.endswith(".sql")


def generated_reason(path: str, attrs: dict[str, str], cfg: Config) -> str | None:
    """Why a file counts as generated or vendored, or None if it does not."""
    if _attribute_set(attrs.get("linguist-generated")):
        return "linguist_generated"
    if _attribute_set(attrs.get("linguist-vendored")):
        return "linguist_vendored"
    name = posixpath.basename(path)
    if name in cfg.lockfiles:
        return "lockfile"
    if any(d in cfg.excluded_dirs for d in _dirs(path)):
        return "excluded_dir"
    if fnmatch.fnmatchcase(name, "*.min.*"):
        return "minified"
    return None


def is_sensitive(path: str, cfg: Config) -> bool:
    tokens = {t for segment in path.lower().split("/") for t in _TOKEN_SPLIT.split(segment)}
    return not tokens.isdisjoint(cfg.sensitive_tokens)


_TEST_NAMES = ("test_*.py", "*_test.py", "*.test.*", "*.spec.*")


def is_test(path: str) -> bool:
    name = posixpath.basename(path)
    dirs = _dirs(path)
    return "tests" in dirs or "__tests__" in dirs or any(fnmatch.fnmatchcase(name, p) for p in _TEST_NAMES)


def classify(path: str, attrs: dict[str, str], jewels: tuple[str, ...], cfg: Config) -> tuple[str, str | None]:
    """The file's class, first match wins, plus the generated reason when the class is `generated`."""
    if is_crown_jewel(path, jewels):
        return CROWN_JEWEL, None
    if is_schema(path):
        return SCHEMA, None
    reason = generated_reason(path, attrs, cfg)
    if reason:
        return GENERATED, reason
    if is_test(path):
        return TEST, None
    if is_sensitive(path, cfg):
        return SENSITIVE, None
    return CODE, None


def select(
    entries: list[TreeEntry],
    attrs: dict[str, dict[str, str]],
    jewels: tuple[str, ...],
    cfg: Config,
    is_binary: Callable[[TreeEntry], bool],
) -> tuple[list[FileRecord], list[tuple[str, str]]]:
    """Splits tracked entries into eligible files and (path, reason) exclusions."""
    eligible, excluded = [], []
    for entry in sorted(entries, key=lambda e: e.path):
        if entry.type != "blob":
            excluded.append((entry.path, "submodule"))
            continue
        if entry.mode == "120000":
            excluded.append((entry.path, "symlink"))
            continue
        if entry.size is not None and entry.size > cfg.max_file_bytes:
            excluded.append((entry.path, "too_large"))
            continue
        cls, reason = classify(entry.path, attrs.get(entry.path, {}), jewels, cfg)
        if cls == GENERATED:
            excluded.append((entry.path, reason))
            continue
        if is_binary(entry):
            excluded.append((entry.path, "binary"))
            continue
        eligible.append(FileRecord(entry.path, entry.sha, entry.size or 0, cls))
    return eligible, excluded
