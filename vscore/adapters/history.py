"""Surviving compressed work W (scoring spec: History extraction; formula /5).

Blame, with copy detection, gives every line at the scanned revision R its origin: the commit that
first wrote it, the path it was written to, and its line number there. From those origins:

- surv(c, p) counts the distinct original lines of path p from commit c that survive anywhere at R,
  over the lines c's whitespace-insensitive diff added to p. Copies of one original line count once,
  and a line blame traces to c that c's diff does not list as added is ignored.
- Each commit's work, info(c, p) * surv(c, p), is counted once in the codebase and shared out to the
  places its lines now sit: an original line found in k places gives each place a weight of 1/k.
- A merge commit is credited with the lines added relative to every parent in its combined diff,
  compressed against the first parent's version of the file. A clean merge adds nothing.

A commit with no surviving lines has surv = 0 and adds nothing, so only commits blame credits are read.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from compression import zstd

from vscore.gitrepo import BlobReader, GitError, Repo, decode_path

CHANGES_FORMAT = "changes-1"  # cache directory; bump when Change or the diff arguments change
ZSTD_MODE = "concatenated-difference"  # info = C(reference + added) - C(reference)
BLAME_BASE = ("blame", "-w", "-M", "--line-porcelain")
DIFF_ARGS = ("diff-tree", "-r", "-M", "-w", "-U0", "--raw", "-p", "--no-abbrev", "--no-commit-id",
             "--src-prefix=a/", "--dst-prefix=b/")
COMBINED_ARGS = ("diff-tree", "-r", "-M", "-w", "--cc", "--no-commit-id")
GITLINK = b"160000"

_C_ESCAPES = {ord("a"): 7, ord("b"): 8, ord("t"): 9, ord("n"): 10, ord("v"): 11, ord("f"): 12, ord("r"): 13,
              ord('"'): 34, ord("\\"): 92}

# One surviving line: (commit that wrote it, path it was written to, its line number there, its line at R).
Origin = tuple[str, str, int, int]


@dataclass(frozen=True)
class Change:
    """What one commit did to one file: where it came from and the lines it added (whitespace ignored)."""

    old_path: str | None
    old_blob: str | None
    added: bytes
    added_lines: int
    added_numbers: frozenset[int] = frozenset()  # line numbers of the added lines in the commit's version
    removed: bytes = b""  # the lines it removed, which a mechanical rewrite's added lines nearly repeat


@dataclass(frozen=True)
class Contribution:
    """One commit's work as it lands in one file at R."""

    commit: str
    path: str  # the path the commit wrote the lines to
    info: int
    added_lines: int
    surviving_lines: int  # distinct original lines surviving anywhere in the codebase
    surv: float
    lines: tuple[tuple[int, float], ...] = ()  # (line at R in this file, weight: 1 / places the original sits)

    @property
    def here(self) -> float:
        return sum(weight for _, weight in self.lines)

    @property
    def work(self) -> float:
        """This file's share of the commit's work, info * surv, by the weight of the lines it holds."""
        return self.info * self.here / self.added_lines if self.added_lines else 0.0


def unquote(raw: bytes) -> str:
    """Undoes git's C-style quoting of unusual paths."""
    if not raw.startswith(b'"'):
        return decode_path(raw)
    out = bytearray()
    i = 1
    while i < len(raw) - 1:
        c = raw[i]
        if c != ord("\\"):
            out.append(c)
            i += 1
        elif raw[i + 1] in _C_ESCAPES:
            out.append(_C_ESCAPES[raw[i + 1]])
            i += 2
        else:
            out.append(int(raw[i + 1 : i + 4], 8))
            i += 4
    return decode_path(bytes(out))


def compressed_size(data: bytes, level: int) -> int:
    options = {
        zstd.CompressionParameter.compression_level: level,
        zstd.CompressionParameter.checksum_flag: 0,
        zstd.CompressionParameter.dict_id_flag: 0,
    }
    return len(zstd.compress(data, options=options))


def info_bytes(added: bytes, reference: bytes, level: int, reference_size: int | None = None) -> int:
    """info = C(reference + added) - C(reference): what the added lines cost to compress on top of the file
    they were added to, with the whole stream compressed together.

    Compressing the stream together lets zstd's symbol statistics cover the reference too, so appended
    work adds up the same however it is split into commits: the sums telescope. (Compressing the added
    lines alone against the reference as a dictionary, as /4 did, charged each small commit for building
    its own statistics: splitting one 30-line commit into 30 one-line commits more than doubled W.)
    """
    before = compressed_size(reference, level) if reference_size is None else reference_size
    return max(0, compressed_size(reference + added, level) - before)


WORD = re.compile(rb"\w+")


def word_change(added: bytes, removed: bytes) -> float:
    """Share of words (runs of letters, digits and underscores) that differ between the added and
    removed lines, counting repeats: 0 when only punctuation and layout changed."""
    a, r = Counter(WORD.findall(added)), Counter(WORD.findall(removed))
    words = sum(a.values()) + sum(r.values())
    return (sum((a - r).values()) + sum((r - a).values())) / words if words else 0.0


def parse_blame_origins(output: bytes) -> list[Origin]:
    """Every line's origin from `git blame --line-porcelain`: (commit, path then, line then, line now)."""
    origins: list[Origin] = []
    sha = filename = None
    original = final = 0
    for line in output.split(b"\n"):
        if sha is None:
            if line:
                fields = line.split(b" ")
                sha, original, final = fields[0].decode(), int(fields[1]), int(fields[2])
        elif line.startswith(b"\t"):
            origins.append((sha, filename, original, final))
            sha = filename = None
        elif line.startswith(b"filename "):
            filename = unquote(line[len(b"filename ") :])
    return origins


def parse_blame(output: bytes) -> dict[tuple[str, str], int]:
    """Lines per (commit, path in that commit) from `git blame --line-porcelain`."""
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for sha, path, _, _ in parse_blame_origins(output):
        counts[sha, path] += 1
    return dict(counts)


def _hunk_start(header: bytes) -> int:
    """The first line number of the result side of a hunk header such as `@@ -3,2 +5,4 @@`."""
    plus = next(part for part in header.split(b" ") if part.startswith(b"+"))
    return int(plus[1:].split(b",")[0])


def parse_diff(output: bytes) -> dict[str, Change]:
    """Changes per new path from `git diff-tree --raw -p -w -U0` output."""
    sources: dict[str, tuple[str | None, str | None]] = {}
    added: dict[str, list[tuple[int, bytes]]] = {}
    removed: dict[str, list[bytes]] = {}
    current: str | None = None
    in_hunk = False
    number = 0
    for line in output.split(b"\n"):
        if line.startswith(b":"):
            meta, *paths = line.split(b"\t")
            old_mode, new_mode, old_sha, _, status = meta[1:].split(b" ")
            kind = status[:1]
            if kind == b"D" or GITLINK in (old_mode, new_mode):  # submodule pointers are not file content
                continue
            names = [unquote(p) for p in paths]
            if kind == b"A":
                sources[names[-1]] = (None, None)
            else:
                sources[names[-1]] = (names[0], old_sha.decode())
        elif line.startswith(b"diff --git "):
            current, in_hunk = None, False
        elif line.startswith(b"+++ ") and not in_hunk:
            target = line[4:]
            current = None if target == b"/dev/null" else unquote(target).removeprefix("b/")
            if current is not None:
                added.setdefault(current, [])
        elif line.startswith(b"@@") and current is not None:
            in_hunk = True
            number = _hunk_start(line)
        elif in_hunk and current is not None and line.startswith(b"+"):
            added[current].append((number, line[1:]))
            number += 1
        elif in_hunk and current is not None and line.startswith(b"-"):
            removed.setdefault(current, []).append(line[1:])
    changes = {}
    for path, (old_path, old_blob) in sources.items():
        lines = added.get(path, [])
        changes[path] = Change(old_path, old_blob, b"".join(text + b"\n" for _, text in lines), len(lines),
                               frozenset(n for n, _ in lines), b"".join(t + b"\n" for t in removed.get(path, [])))
    return changes


def parse_combined(output: bytes) -> dict[str, list[tuple[int, bytes]]]:
    """Lines a merge added relative to every parent, per path, from `git diff-tree --cc` output.

    Each hunk line starts with one column per parent. A line with `-` in any column is not in the
    result; a line with `+` in every column differs from all parents, so the merge itself wrote it.
    """
    introduced: dict[str, list[tuple[int, bytes]]] = {}
    current: str | None = None
    parents = 0
    number = 0
    in_hunk = False
    for line in output.split(b"\n"):
        if line.startswith(b"diff --cc ") or line.startswith(b"diff --combined "):
            current = unquote(line.split(b" ", 2)[2])
            introduced.setdefault(current, [])
            in_hunk = False
        elif line.startswith(b"@@") and current is not None:
            parents = len(line) - len(line.lstrip(b"@")) - 1
            number = _hunk_start(line)
            in_hunk = True
        elif in_hunk and current is not None and len(line) >= parents and parents:
            columns = line[:parents]
            if b"-" in columns:
                continue
            if columns == b"+" * parents:
                introduced[current].append((number, line[parents:]))
            number += 1
    return {path: lines for path, lines in introduced.items() if lines}


class History:
    """Reads blame and per-commit diffs for one revision, caching blame by revision and file."""

    def __init__(self, repo: Repo, rev: str, blobs: BlobReader, level: int, ignore_revs: bytes | None,
                 jobs: int | None = None, copy_detection: int = 3, mechanical_min_bytes: int = 200,
                 mechanical_words: float = 0.05):
        self.repo = repo
        self.rev = rev
        self.blobs = blobs
        self.level = level
        self.jobs = jobs or min(32, os.cpu_count() or 4)
        self.blame_args = BLAME_BASE + ("-C",) * copy_detection
        self.mechanical_min_bytes = mechanical_min_bytes
        self.mechanical_words = mechanical_words
        self.mechanical: list[str] = []  # commits blame looked through, reported in the scan's coverage
        self.blame_revs: dict[str, str] = {}  # path -> the commit blame runs at (last_changes)
        self._reference_sizes: dict[str, int] = {}
        self.cache = repo.git_dir / "vscore-cache"
        self.ignore_args: tuple[str, ...] = ()
        self.ignore_digest = "none"
        if ignore_revs is not None:
            self.ignore_digest = hashlib.sha256(ignore_revs).hexdigest()[:16]
            ignore_file = self.cache / "ignore-revs" / self.ignore_digest
            ignore_file.parent.mkdir(parents=True, exist_ok=True)
            ignore_file.write_bytes(ignore_revs)
            self.ignore_args = ("--ignore-revs-file", str(ignore_file))
        self._empty_tree = repo.empty_tree()

    def last_changes(self, paths: list[str]) -> dict[str, str]:
        """For each path, the newest commit on the revision's first-parent line that changed it relative
        to its first parent. Blame passes every line of a file a commit left unchanged to the first
        parent with the same content, so blame at that commit equals blame at the revision, and a scan of
        a later revision reuses it for every file the new commits did not touch."""
        output = self.repo.run("log", "--first-parent", "--diff-merges=first-parent", "--no-renames",
                               "--format=%x00%H", "--name-only", self.rev)
        pending, found = set(paths), {}
        for chunk in output.split(b"\0")[1:]:
            sha, *names = chunk.split(b"\n")
            for name in (unquote(raw) for raw in names if raw):
                if name in pending:
                    found[name] = sha.decode()
                    pending.discard(name)
            if not pending:
                break
        return found

    def blame(self, path: str, skip: tuple[str, ...] = ()) -> list[Origin] | None:
        """Every line's origin in one file at the revision, looking through the commits in `skip`;
        None if blame fails."""
        skip_args = tuple(arg for sha in skip for arg in ("--ignore-rev", sha))
        rev = self.blame_revs.get(path, self.rev)
        parts = (rev, path, self.ignore_digest, "origins", *self.blame_args, *skip_args)
        cached = self.cache / "blame" / rev[:2] / f"{hashlib.sha256(chr(0).join(parts).encode()).hexdigest()}.json"
        if cached.exists():
            return [tuple(row) for row in json.loads(cached.read_text())]
        try:
            output = self.repo.run(*self.blame_args, *self.ignore_args, *skip_args, rev, "--", path)
            found = parse_blame_origins(output)
        except GitError:
            return None
        cached.parent.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps([list(row) for row in found]))
        tmp.replace(cached)
        return found

    def changes(self, commit: str, parents: tuple[str, ...]) -> dict[str, Change]:
        """What a commit added to each path, cached by commit: a commit's diff never changes."""
        cached = self.cache / CHANGES_FORMAT / commit[:2] / f"{commit}.json"
        if cached.exists():
            return {path: Change(old_path, old_blob, base64.b64decode(added), n, frozenset(numbers),
                                 base64.b64decode(removed))
                    for path, (old_path, old_blob, added, n, numbers, removed) in json.loads(cached.read_text()).items()}
        found = self._changes(commit, parents)
        cached.parent.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps({path: [c.old_path, c.old_blob, base64.b64encode(c.added).decode(), c.added_lines,
                                          sorted(c.added_numbers), base64.b64encode(c.removed).decode()]
                                   for path, c in found.items()}))
        tmp.replace(cached)
        return found

    def _changes(self, commit: str, parents: tuple[str, ...]) -> dict[str, Change]:
        """For a merge: only the lines new relative to every parent, with the first parent's version of
        the file as the reference."""
        base = parents[0] if parents else self._empty_tree
        first_parent = parse_diff(self.repo.run(*DIFF_ARGS, base, commit))
        if len(parents) < 2:
            return first_parent
        introduced = parse_combined(self.repo.run(*COMBINED_ARGS, commit))
        changes = {}
        for path, lines in introduced.items():
            source = first_parent.get(path)
            if source is None:  # unchanged from the first parent (or a submodule): nothing new was written
                continue
            changes[path] = Change(source.old_path, source.old_blob, b"".join(text + b"\n" for _, text in lines),
                                   len(lines), frozenset(n for n, _ in lines))
        return changes

    def info(self, change: Change) -> int:
        reference = self.blobs.read(change.old_blob) if change.old_blob else b""
        key = change.old_blob or "empty"
        if key not in self._reference_sizes:
            self._reference_sizes[key] = compressed_size(reference, self.level)
        return info_bytes(change.added, reference, self.level, self._reference_sizes[key])

    def mechanical_paths(self, parents: tuple[str, ...], changes: dict[str, Change]) -> set[str]:
        """The paths a non-merge commit rewrote mechanically, as a formatter or linter does: at least
        `mechanical_min_bytes` added, and almost every word (identifier, literal, keyword) of the added
        and removed lines the same, so only punctuation and layout changed. Judged per path, because a
        reformat often lands with real edits to other files, such as the formatter's configuration."""
        if len(parents) != 1:
            return set()
        return {path for path, c in changes.items() if len(c.added) >= self.mechanical_min_bytes
                and word_change(c.added, c.removed) <= self.mechanical_words}

    def contributions(self, paths: list[str]) -> dict[str, list[Contribution] | None]:
        """Per file, the commits whose original lines survive in it; None where blame failed."""
        with ThreadPoolExecutor(self.jobs) as pool:
            self.blame_revs = self.last_changes(paths)
            blames = dict(zip(paths, pool.map(self.blame, paths)))
            commits = sorted({row[0] for rows in blames.values() if rows for row in rows})
            parents = self.repo.parents(commits)
            diffs = dict(zip(commits, pool.map(lambda c: self.changes(c, parents.get(c, ())), commits)))

            # Look through mechanical rewrites: re-blame, once, each file credited with a line a commit
            # rewrote mechanically, skipping every commit that rewrote a path the file's lines came from.
            mechanical = {c: found for c in commits
                          if (found := self.mechanical_paths(parents.get(c, ()), diffs[c]))}
            self.mechanical = sorted(mechanical)
            skips: dict[str, tuple[str, ...]] = {}
            for p in paths:
                rows = blames[p] or ()
                if any(row[1] in mechanical.get(row[0], ()) for row in rows):
                    seen = {p} | {row[1] for row in rows}
                    skips[p] = tuple(sorted(c for c, found in mechanical.items() if found & seen))
            if skips:
                touched = sorted(skips)
                reblamed = pool.map(lambda p: self.blame(p, skips[p]), touched)
                blames.update(zip(touched, reblamed))
                new = sorted({row[0] for p in touched if blames[p] for row in blames[p]} - set(diffs))
                parents.update(self.repo.parents(new))
                diffs.update(zip(new, pool.map(lambda c: self.changes(c, parents.get(c, ())), new)))

        places: dict[tuple[str, str, int], list[tuple[str, int]]] = defaultdict(list)
        for path in paths:
            for sha, origin_path, original, final in blames[path] or ():
                change = diffs.get(sha, {}).get(origin_path)
                if change is not None and original in change.added_numbers:
                    places[sha, origin_path, original].append((path, final))

        distinct: dict[tuple[str, str], int] = defaultdict(int)
        held: dict[str, dict[tuple[str, str], list[tuple[int, float]]]] = defaultdict(lambda: defaultdict(list))
        for (sha, origin_path, _), where in places.items():
            distinct[sha, origin_path] += 1
            for path, final in where:
                held[path][sha, origin_path].append((final, 1 / len(where)))

        info_cache: dict[tuple[str, str], int] = {}
        result: dict[str, list[Contribution] | None] = {}
        for path in paths:
            if blames[path] is None:
                result[path] = None
                continue
            found = []
            for (sha, origin_path), lines in sorted(held[path].items()):
                change = diffs[sha][origin_path]
                if (sha, origin_path) not in info_cache:
                    info_cache[sha, origin_path] = self.info(change)
                count = distinct[sha, origin_path]
                found.append(Contribution(sha, origin_path, info_cache[sha, origin_path], change.added_lines,
                                          count, count / change.added_lines, tuple(sorted(lines))))
            result[path] = found
        return result
