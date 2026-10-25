"""Thin wrapper around the git command line, with output-affecting settings pinned for reproducibility."""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

# Settings that change diff, blame or path output. Pinned so neither user nor repo config can alter scores.
_PINNED = (
    "core.quotepath=false",
    "diff.algorithm=myers",
    "diff.renameLimit=1000",
    "diff.noprefix=false",
    "diff.mnemonicPrefix=false",
    "blame.ignoreRevsFile=",
    "blame.markIgnoredLines=false",
    "blame.markUnblamableLines=false",
)


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class TreeEntry:
    path: str
    mode: str
    type: str
    sha: str
    size: int | None


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", LC_ALL="C", GIT_TERMINAL_PROMPT="0")
    return env


def decode_path(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def encode_path(path: str) -> bytes:
    return path.encode("utf-8", "surrogateescape")


class Repo:
    _git_dirs: dict[Path, Path] = {}  # working tree -> its .git directory, found once per process

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.env = _env()
        known = Repo._git_dirs.get(self.path)
        if known is None or not known.is_dir():
            known = Repo._git_dirs[self.path] = Path(self.run("rev-parse", "--absolute-git-dir").decode().strip())
        self.git_dir = known

    def command(self, *args: str) -> list[str]:
        cmd = ["git", "-C", str(self.path)]
        for setting in _PINNED:
            cmd += ["-c", setting]
        return [*cmd, *args]

    def run(self, *args: str, input: bytes | None = None) -> bytes:
        proc = subprocess.run(self.command(*args), input=input, capture_output=True, env=self.env)
        if proc.returncode != 0:
            message = proc.stderr.decode(errors="replace").strip()
            raise GitError(f"git {' '.join(args[:2])} failed: {message}")
        return proc.stdout

    def version(self) -> str:
        return self.run("version").decode().strip().removeprefix("git version ")

    def resolve(self, rev: str) -> str:
        return self.run("rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}").decode().strip()

    def is_shallow(self) -> bool:
        return self.run("rev-parse", "--is-shallow-repository").strip() == b"true"

    def empty_tree(self) -> str:
        return self.run("hash-object", "-t", "tree", "--stdin", input=b"").decode().strip()

    def ls_tree(self, rev: str) -> list[TreeEntry]:
        entries = []
        for item in self.run("ls-tree", "-r", "-z", "--long", "--full-tree", rev).split(b"\0"):
            if not item:
                continue
            meta, path = item.split(b"\t", 1)
            mode, kind, sha, size = meta.split()
            entries.append(
                TreeEntry(decode_path(path), mode.decode(), kind.decode(), sha.decode(), None if size == b"-" else int(size))
            )
        return entries

    def attributes(self, rev: str, paths: list[str], names: tuple[str, ...]) -> dict[str, dict[str, str]]:
        """Git attribute values per path, read from the .gitattributes files in `rev`."""
        if not paths:
            return {}
        stdin = b"".join(encode_path(p) + b"\0" for p in paths)
        out = self.run("check-attr", f"--source={rev}", "-z", "--stdin", *names, input=stdin)
        fields = out.split(b"\0")
        result: dict[str, dict[str, str]] = {}
        for i in range(0, len(fields) - 2, 3):
            path, name, value = (decode_path(f) for f in fields[i : i + 3])
            result.setdefault(path, {})[name] = value
        return result

    def parents(self, commits: list[str]) -> dict[str, tuple[str, ...]]:
        """Parent commits of each commit; a shallow-clone boundary reports none."""
        if not commits:
            return {}
        stdin = "".join(f"{c}\n" for c in commits).encode()
        out = self.run("rev-list", "--no-walk=unsorted", "--parents", "--stdin", input=stdin)
        result = {}
        for line in out.decode().splitlines():
            sha, *parents = line.split()
            result[sha] = tuple(parents)
        return result


class BlobReader:
    """Reads object contents through one long-running `git cat-file --batch`."""

    def __init__(self, repo: Repo):
        self._proc = subprocess.Popen(
            repo.command("cat-file", "--batch"), stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=repo.env
        )
        self._lock = threading.Lock()

    def read(self, sha: str) -> bytes:
        with self._lock:
            self._proc.stdin.write(sha.encode() + b"\n")
            self._proc.stdin.flush()
            header = self._proc.stdout.readline().split()
            if len(header) < 3 or header[1] == b"missing":
                raise GitError(f"object {sha} is missing")
            data = self._proc.stdout.read(int(header[2]))
            self._proc.stdout.read(1)
            return data

    def close(self) -> None:
        self._proc.stdin.close()
        self._proc.wait()

    def __enter__(self) -> BlobReader:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
