"""Surviving compressed work W(f) (scoring spec v1: History extraction).

W(f) sums info(c, f) * surv(c, f) over non-merge commits c. A commit with no lines left in f has
surv = 0 and adds nothing, so only commits that blame attributes lines to at the scanned revision
need to be read. Blame also follows file renames, which gives the rename map: for each surviving
line it reports the commit and the file's path in that commit.
"""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from compression import zstd

from vscore.gitrepo import BlobReader, GitError, Repo, decode_path

ZSTD_MODE = "raw-content-prefix"
BLAME_ARGS = ("blame", "-w", "-M", "--line-porcelain")
DIFF_ARGS = ("diff-tree", "-r", "-M", "-w", "-U0", "--raw", "-p", "--no-abbrev", "--no-commit-id",
             "--src-prefix=a/", "--dst-prefix=b/")

_C_ESCAPES = {ord("a"): 7, ord("b"): 8, ord("t"): 9, ord("n"): 10, ord("v"): 11, ord("f"): 12, ord("r"): 13,
              ord('"'): 34, ord("\\"): 92}


@dataclass(frozen=True)
class Change:
    """What one commit did to one file: where it came from and the lines it added (whitespace ignored)."""

    old_path: str | None
    old_blob: str | None
    added: bytes
    added_lines: int


@dataclass(frozen=True)
class Contribution:
    commit: str
    path: str
    info: int
    added_lines: int
    surviving_lines: int
    surv: float
    lines: tuple[int, ...] = ()  # where the surviving lines sit in the file at the scanned revision

    @property
    def work(self) -> float:
        return self.info * self.surv

    def work_in(self, start: int, end: int) -> float:
        """The share of this commit's work whose surviving lines fall in [start, end]."""
        inside = sum(1 for n in self.lines if start <= n <= end)
        return self.info * min(1.0, inside / self.added_lines) if self.added_lines else 0.0


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


def info_bytes(added: bytes, reference: bytes, level: int) -> int:
    """Compressed size of `added` given `reference` as a zstd raw-content prefix, minus the empty-input size."""
    options = {
        zstd.CompressionParameter.compression_level: level,
        zstd.CompressionParameter.checksum_flag: 0,
        zstd.CompressionParameter.dict_id_flag: 0,
    }
    prefix = zstd.ZstdDict(reference, is_raw=True).as_prefix if reference else None
    size = len(zstd.compress(added, options=options, zstd_dict=prefix))
    empty = len(zstd.compress(b"", options=options, zstd_dict=prefix))
    return max(0, size - empty)


def parse_blame_lines(output: bytes) -> dict[tuple[str, str], list[int]]:
    """Surviving line numbers per (commit, path in that commit) from `git blame --line-porcelain`."""
    lines: dict[tuple[str, str], list[int]] = {}
    sha = filename = None
    final = 0
    for line in output.split(b"\n"):
        if sha is None:
            if line:
                fields = line.split(b" ")
                sha, final = fields[0].decode(), int(fields[2])
        elif line.startswith(b"\t"):
            lines.setdefault((sha, filename), []).append(final)
            sha = filename = None
        elif line.startswith(b"filename "):
            filename = unquote(line[len(b"filename ") :])
    return lines


def parse_blame(output: bytes) -> dict[tuple[str, str], int]:
    """Lines per (commit, path in that commit) from `git blame --line-porcelain`."""
    return {key: len(found) for key, found in parse_blame_lines(output).items()}


def parse_diff(output: bytes) -> dict[str, Change]:
    """Changes per new path from `git diff-tree --raw -p -w -U0` output."""
    sources: dict[str, tuple[str | None, str | None]] = {}
    added: dict[str, list[bytes]] = {}
    current: str | None = None
    in_hunk = False
    for line in output.split(b"\n"):
        if line.startswith(b":"):
            meta, *paths = line.split(b"\t")
            _, _, old_sha, _, status = meta[1:].split(b" ")
            kind = status[:1]
            if kind == b"D":
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
        elif line.startswith(b"@@"):
            in_hunk = True
        elif in_hunk and current is not None and line.startswith(b"+"):
            added[current].append(line[1:])
    changes = {}
    for path, (old_path, old_blob) in sources.items():
        lines = added.get(path, [])
        changes[path] = Change(old_path, old_blob, b"".join(line + b"\n" for line in lines), len(lines))
    return changes


class History:
    """Reads blame and per-commit diffs for one revision, caching blame by revision and file."""

    def __init__(self, repo: Repo, rev: str, blobs: BlobReader, level: int, ignore_revs: bytes | None,
                 jobs: int | None = None):
        self.repo = repo
        self.rev = rev
        self.blobs = blobs
        self.level = level
        self.jobs = jobs or min(32, os.cpu_count() or 4)
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

    def blame(self, path: str) -> dict[tuple[str, str], list[int]] | None:
        """Surviving line numbers per (commit, path then); None if blame fails."""
        key = hashlib.sha256("\0".join((self.rev, path, self.ignore_digest, "lines", *BLAME_ARGS)).encode()).hexdigest()
        cached = self.cache / "blame" / self.rev / f"{key}.json"
        if cached.exists():
            return {(sha, name): lines for sha, name, lines in json.loads(cached.read_text())}
        try:
            found = parse_blame_lines(self.repo.run(*BLAME_ARGS, *self.ignore_args, self.rev, "--", path))
        except GitError:
            return None
        cached.parent.mkdir(parents=True, exist_ok=True)
        rows = sorted([sha, name, lines] for (sha, name), lines in found.items())
        tmp = cached.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(rows))
        tmp.replace(cached)
        return found

    def changes(self, commit: str, parents: tuple[str, ...]) -> dict[str, Change]:
        base = parents[0] if parents else self._empty_tree
        return parse_diff(self.repo.run(*DIFF_ARGS, base, commit))

    def contributions(self, paths: list[str]) -> dict[str, list[Contribution] | None]:
        """Per file, the commits whose added lines survive at the revision; None where blame failed."""
        with ThreadPoolExecutor(self.jobs) as pool:
            blames = dict(zip(paths, pool.map(self.blame, paths)))
            commits = sorted({sha for counts in blames.values() if counts for sha, _ in counts})
            parents = self.repo.parents(commits)
            non_merge = [c for c in commits if len(parents.get(c, ())) <= 1]
            diffs = dict(zip(non_merge, pool.map(lambda c: self.changes(c, parents.get(c, ())), non_merge)))

        info_cache: dict[tuple[str, str], int] = {}
        result: dict[str, list[Contribution] | None] = {}
        for path in paths:
            counts = blames[path]
            if counts is None:
                result[path] = None
                continue
            found = []
            for (sha, name), lines in sorted(counts.items()):
                change = diffs.get(sha, {}).get(name)
                if change is None or change.added_lines == 0:
                    continue
                if (sha, name) not in info_cache:
                    reference = self.blobs.read(change.old_blob) if change.old_blob else b""
                    info_cache[sha, name] = info_bytes(change.added, reference, self.level)
                surv = min(1.0, len(lines) / change.added_lines)
                found.append(Contribution(sha, name, info_cache[sha, name], change.added_lines, len(lines), surv,
                                          tuple(lines)))
            result[path] = found
        return result
