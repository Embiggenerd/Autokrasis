"""Builds small Git repositories with fixed authors and dates, for tests and the fixture app."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_EPOCH = 1_704_067_200  # 2024-01-01T00:00:00Z


class GitFixture:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._tick = 0
        self.git("init", "-q", "-b", "main")

    def _env(self) -> dict[str, str]:
        stamp = f"{_EPOCH + self._tick * 3600} +0000"
        env = dict(os.environ)
        env.update(
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="Fixture",
            GIT_AUTHOR_EMAIL="fixture@example.com",
            GIT_COMMITTER_NAME="Fixture",
            GIT_COMMITTER_EMAIL="fixture@example.com",
            GIT_AUTHOR_DATE=stamp,
            GIT_COMMITTER_DATE=stamp,
        )
        return env

    def git(self, *args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=self.path, env=self._env(), capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {proc.stderr}")
        return proc.stdout.strip()

    def write(self, rel: str, content: str | bytes) -> None:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)

    def remove(self, rel: str) -> None:
        self.git("rm", "-q", rel)

    def move(self, old: str, new: str) -> None:
        (self.path / new).parent.mkdir(parents=True, exist_ok=True)
        self.git("mv", old, new)

    def commit(self, message: str) -> str:
        self._tick += 1
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def checkout(self, branch: str, create: bool = False) -> None:
        self.git("checkout", "-q", *(["-b"] if create else []), branch)

    def merge(self, branch: str, message: str, resolve: dict[str, str] | None = None) -> str:
        """Merges with --no-ff; `resolve` gives final contents for files left in conflict."""
        self._tick += 1
        if resolve is None:
            self.git("merge", "-q", "--no-ff", "-m", message, branch)
        else:
            subprocess.run(["git", "merge", "-q", "--no-ff", branch], cwd=self.path, env=self._env(),
                           capture_output=True)
            for rel, content in resolve.items():
                self.write(rel, content)
            self.git("add", "-A")
            self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")
