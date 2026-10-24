"""Builds scenario repositories with fixed authors and dates, so every run starts from identical history."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

_EPOCH = 1_704_067_200  # 2024-01-01T00:00:00Z


class ScenarioRepo:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._tick = 0
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        stamp = f"{_EPOCH + self._tick * 3600} +0000"
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com", "GIT_COMMITTER_NAME": "Dev",
               "GIT_COMMITTER_EMAIL": "dev@example.com", "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
        return subprocess.run(["git", *args], cwd=self.path, env=env, check=True, capture_output=True, text=True).stdout

    def write(self, files: dict[str, str]) -> None:
        for rel, content in files.items():
            target = self.path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

    def commit(self, message: str, files: dict[str, str] | None = None) -> None:
        if files:
            self.write(files)
        self._tick += 1
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def database(self, rel: str, script: str, rows: dict[str, list[tuple]]) -> Path:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(target)
        db.executescript(script)
        for table, values in rows.items():
            if values:
                db.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(values[0]))})", values)
        db.commit()
        db.close()
        return target
