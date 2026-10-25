"""Scan settings. Defaults follow the v1 scoring spec; a TOML file may override them."""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path


@dataclass(frozen=True)
class Config:
    alpha: float = 2.0
    zstd_level: int = 9
    max_file_bytes: int = 1_048_576
    lockfiles: tuple[str, ...] = (
        "poetry.lock",
        "package-lock.json",
        "yarn.lock",
        "Pipfile.lock",
        "pnpm-lock.yaml",
        "uv.lock",
    )
    excluded_dirs: tuple[str, ...] = ("vendor", "node_modules", "dist", "build")
    sensitive_tokens: tuple[str, ...] = ("auth", "payment", "payments", "billing", "user", "users", "pii")
    import_roots: tuple[str, ...] = ("", "src")
    # How many -C flags blame gets: 3 finds copies from any commit, 2 only from the commit that created the
    # file. 2 ships: a cold Flask scan took 32.6 s with 3 and 25.8 s with 2, against the release's 30 s limit,
    # and total W differed by 0.2%. Code copied into an existing file from an unrelated file counts as new.
    copy_detection: int = 2
    # A commit's change to a file is mechanical (a reformat, a lint fix) when it keeps the words of the
    # lines it replaces and changes only punctuation and layout: blame looks through it, as if it were in
    # .git-blame-ignore-revs. Chosen on 3,078 candidate commits of five public repositories.
    mechanical_words: float = 0.05  # at most this share of the added and removed words differ
    mechanical_min_bytes: int = 200  # and at least this many added bytes, so small real edits are never skipped
    crown_jewels_file: str = "crown_jewels.yml"
    blame_ignore_revs_file: str = ".git-blame-ignore-revs"

    # vscore compare: an action may destroy at most this many times the W of the code the bug is about,
    # with the bug's W floored so a bug in a tiny or new file still allows small changes.
    compare_limit: float = 2.0
    compare_floor_w: float = 500.0

    @classmethod
    def load(cls, path: str | Path | None) -> Config:
        """Reads overrides from the `[vscore]` table of a TOML file; no path means defaults."""
        if path is None:
            return cls()
        table = tomllib.loads(Path(path).read_text()).get("vscore", {})
        known = {f.name: f for f in fields(cls)}
        unknown = sorted(set(table) - set(known))
        if unknown:
            raise ValueError(f"unknown config keys: {', '.join(unknown)}")
        values = {k: tuple(v) if isinstance(v, list) else v for k, v in table.items()}
        return replace(cls(), **values)

    def digest(self) -> str:
        """Hash of every setting, recorded in each scan so results can be traced to their configuration."""
        canonical = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]
