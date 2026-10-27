"""vscore compare: the W of the code a bug is about against the W an action would destroy.

A person fixing a bug in one file would never delete the codebase to do it. This check gives an agent the
same sense of proportion. W is surviving compressed work in bytes (docs/plan.md, Scoring spec):

- The bug's W is the sum over the files and symbols it names; a directory counts every file under it.
- The action's W is what it would delete or replace: files deleted, overwritten or mostly rewritten (with
  their uncommitted work), top-level functions and classes removed, uncommitted work discarded.

- A SQLite table's W is the compressed size of its current rows, an experimental proxy for the work in
  the data; losing the table, or the database file, destroys it. Rows deleted by a `WHERE` count as 0.

The action runs when it destroys at most `compare_limit` times the bug's W (floored at `compare_floor_w`,
so a bug in a tiny or new file still allows small changes). Losses W cannot weigh, such as tables of other
databases, tables without a rowid, rewriting Git history, or removing files a pipe names, stop it.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from vscore import FORMULA_VERSION
from vscore.actions import Context, from_tool_call, malformed
from vscore.adapters.history import compressed_size, info_bytes
from vscore.audit import AuditLog
from vscore.config import Config
from vscore.gitrepo import GitError, Repo, decode_path
from vscore.scan import scan

MATCHER = "Bash|Write|Edit|MultiEdit|NotebookEdit"

_LOSES = {"delete", "overwrite", "rewrite", "unresolved"}
_TABLE_LOSS = {"drop", "db_reset", "truncate", "bulk_delete", "bulk_update", "drop_column"}
_UNCOMMITTED = {"git:worktree", "git:untracked", "git:stash"}


@dataclass
class Comparison:
    bug: list[str]
    bug_w: float
    destroyed: float
    codebase: float
    limit: float
    floor: float
    summary: str
    files: list[tuple[str, float]] = field(default_factory=list)  # what the action destroys, largest first
    unmeasured: list[str] = field(default_factory=list)  # losses W cannot weigh

    @property
    def ratio(self) -> float:
        return self.destroyed / max(self.bug_w, self.floor)

    @property
    def allowed(self) -> bool:
        return not self.unmeasured and self.ratio <= self.limit

    def message(self) -> str:
        bug = f"the bug ({', '.join(self.bug)}) is worth W {self.bug_w:,.0f}" if self.bug else (
            f"no bug is named, so W {self.floor:,.0f} is the allowance")
        lost = f"this destroys W {self.destroyed:,.0f}"
        if self.files:
            lost += " (" + ", ".join(f"{p} {w:,.0f}" for p, w in self.files[:3]) + (", ..." if len(self.files) > 3 else "") + ")"
        text = f"vscore: {bug}; {lost}, {self.ratio:.1f}x the bug, of a codebase worth W {self.codebase:,.0f}."
        if self.unmeasured:
            return text + f" It also {'; '.join(self.unmeasured)}, which W cannot weigh against code: stop."
        if not self.allowed:
            return text + f" That is more than {self.limit:g}x what the bug is worth: stop and make a narrower fix."
        return text

    def to_dict(self) -> dict:
        return {"bug": self.bug, "bug_w": round(self.bug_w, 1), "destroyed": round(self.destroyed, 1),
                "codebase": round(self.codebase, 1), "ratio": round(self.ratio, 2), "limit": self.limit,
                "allowed": self.allowed, "summary": self.summary, "files": [[p, round(w, 1)] for p, w in self.files],
                "unmeasured": self.unmeasured}


def scores(repo_root: str, cfg: Config) -> dict:
    """W of every file and symbol at HEAD, from the scan, kept per revision so later checks are instant."""
    repo = Repo(repo_root)
    revision = repo.resolve("HEAD")
    cached = repo.git_dir / "vscore-cache" / "scores" / f"{revision}-{FORMULA_VERSION.rsplit('/', 1)[-1]}-{cfg.digest()}.json"
    if cached.exists():
        return json.loads(cached.read_text())
    data = scan(repo_root, "HEAD", cfg).data
    found = {"files": {f["path"]: f["W"] or 0.0 for f in data["files"]},
             "symbols": {f"{s['path']}::{s['name']}": s["W"] or 0.0 for s in data["symbols"]},
             "total": data["total"]["W"] or 0.0}
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(found))
    return found


def _bug_w(bug: list[str], known: dict) -> float:
    """W of what the bug names: paths, globs and directories, or symbols as path::name."""
    total, counted = 0.0, set()
    for name in bug:
        name = name.removeprefix("file:").removeprefix("sym:")
        if "::" in name:
            if name not in counted:
                counted.add(name)
                total += known["symbols"].get(name, 0.0)
            continue
        prefix = name.rstrip("/") + "/"
        for path, w in known["files"].items():
            if path not in counted and (path == name or path.startswith(prefix) or fnmatch.fnmatchcase(path, name)):
                counted.add(path)
                total += w
    return total


def compare(repo_root, bug: list[str], tool_name: str, tool_input: dict, cwd: str | None = None,
            cfg: Config | None = None) -> Comparison:
    cfg = cfg or Config()
    root = str(Repo(repo_root).path)
    known = scores(root, cfg)
    repo = Repo(root)
    tracked = {decode_path(p) for p in repo.run("ls-files", "-z").split(b"\0") if p}
    ctx = Context(repo_root=root, cwd=cwd or root, tracked=tracked)
    action = from_tool_call(tool_name, tool_input, ctx)
    lost: dict[str, float] = {}
    unmeasured: list[str] = []
    gone = {t.node[5:] for t in action.targets if t.node.startswith("file:") and t.effect in _LOSES}
    for target in action.targets:
        node = target.node
        if node.startswith("file:") and node != "file:?" and target.effect in _LOSES:
            path = node[5:]
            lost[path] = known["files"].get(path, 0.0) + _uncommitted(repo, cfg, [path])
        elif node.startswith("sym:") and target.effect == "delete" and node[4:].split("::")[0] not in gone:
            lost[node[4:]] = known["symbols"].get(node[4:], 0.0)
        elif node in _UNCOMMITTED:
            paths = target.detail.split() if node == "git:worktree" and not target.detail.startswith("git") else None
            lost[f"uncommitted work ({node[4:]})"] = _uncommitted(repo, cfg, paths, untracked=node == "git:untracked")
        elif node.startswith("table:") and target.effect in _TABLE_LOSS:
            database, _, table = node[6:].split(".", 2)
            paths = [path for path, name in ctx.databases.items() if name == database]
            if not paths:
                unmeasured.append(f"would {target.effect.replace('_', ' ')} {node[6:]}")
            for path in paths:
                for label, w in _tables_w(path, table, cfg.zstd_level, ctx.rel(path) or path):
                    if w is None:
                        unmeasured.append(label)
                    else:
                        lost[label] = w
        elif node == "git:history":
            unmeasured.append("rewrites Git history")
        elif node in ("file:?", "action:?") or target.effect == "unsupported":
            unmeasured.append(f"runs something vscore cannot read ({target.detail[:60]})")
    files = sorted(((p, w) for p, w in lost.items() if w > 0), key=lambda pw: -pw[1])
    return Comparison(list(bug), _bug_w(bug, known), sum(lost.values()), known["total"], cfg.compare_limit,
                      cfg.compare_floor_w, action.summary, files, list(dict.fromkeys(unmeasured)))


def _tables_w(path: str, table: str, level: int, shown: str) -> list[tuple[str, float | None]]:
    """W of one table of a SQLite file, or of every table for `*`: the zstd-compressed size of its rows,
    each a JSON array of its values in rowid order (blobs as hex), one per line, read from a read-only
    snapshot. A table without a rowid, or one that cannot be read, has no W (None)."""
    found = []
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return [(f"would lose {shown}, which could not be read", None)]
    try:
        db.execute("BEGIN")
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                          "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for name in names if table == "*" else [n for n in names if n.lower() == table]:
            try:
                rows = db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
            except sqlite3.OperationalError:
                found.append((f"would lose table {name} in {shown}, which has no rowid", None))
                continue
            text = "".join(json.dumps([v.hex() if isinstance(v, bytes) else v for v in row], ensure_ascii=False) + "\n"
                           for row in rows)
            count = f"{len(rows):,} row{'' if len(rows) == 1 else 's'}"
            found.append((f"{shown}: table {name} ({count})", float(compressed_size(text.encode(), level))))
    except sqlite3.Error:
        found.append((f"would lose {shown}, which could not be read", None))
    finally:
        db.close()
    return found


def _uncommitted(repo: Repo, cfg: Config, paths: list[str] | None, untracked: bool = False) -> float:
    """Work Git cannot bring back, uncommitted edits or untracked files, in the same compressed bytes as W."""
    if untracked:
        total = 0
        for name in (decode_path(p) for p in repo.run("ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if p):
            try:
                total += info_bytes((repo.path / name).read_bytes()[: cfg.max_file_bytes], b"", cfg.zstd_level)
            except OSError:
                continue
        return float(total)
    try:
        diff = repo.run("diff", "HEAD", "-w", "-U0", "--no-color", "--", *(paths or []))
    except GitError:
        return 0.0
    added = b"".join(line[1:] + b"\n" for line in diff.split(b"\n") if line.startswith(b"+") and not line.startswith(b"+++"))
    return float(info_bytes(added, b"", cfg.zstd_level)) if added else 0.0


def bug_for(root: str) -> list[str]:
    """The bug's files: VSCORE_BUG (comma-separated), or the `nodes` of .vscore/report.json."""
    if names := os.environ.get("VSCORE_BUG"):
        return [n.strip() for n in names.split(",") if n.strip()]
    report = Path(os.environ.get("VSCORE_REPORT") or Path(root) / ".vscore" / "report.json")
    if report.exists():
        return [n for n in json.loads(report.read_text()).get("nodes", []) if not n.startswith("table:")]
    return []


def repo_root(cwd: str) -> str | None:
    try:
        return Repo(cwd).run("rev-parse", "--show-toplevel").decode().strip()
    except (GitError, FileNotFoundError, NotADirectoryError):
        return None


def load_config(root: str) -> Config:
    """`.vscore/config.toml`, or the file VSCORE_CONFIG names."""
    path = Path(os.environ.get("VSCORE_CONFIG") or Path(root) / ".vscore" / "config.toml")
    return Config.load(path if path.exists() else None)


def settings_snippet() -> str:
    """The `.claude/settings.json` entry that runs the hook before every Bash, Write and Edit call."""
    snippet = {"hooks": {"PreToolUse": [{"matcher": MATCHER, "hooks": [{"type": "command",
                                                                        "command": "vscore hook claude-code"}]}]}}
    return json.dumps(snippet, indent=2) + "\n"


def hook(payload: dict) -> dict | None:
    """Claude Code PreToolUse hook: silent when the action is proportionate to the bug, a deny with both
    numbers when it is not. It never grants permission."""
    cwd = payload.get("cwd") or os.getcwd()
    root = repo_root(cwd)
    tool_name, tool_input = payload.get("tool_name", ""), payload.get("tool_input") or {}
    if root is None or malformed(tool_name, tool_input):
        return None
    result = compare(root, bug_for(root), tool_name, tool_input, cwd, load_config(root))
    AuditLog(Repo(root).git_dir / "vscore-cache" / "compare.jsonl").append({"at": datetime.now(UTC).isoformat(timespec="seconds"),
                                                "policy": "compare", "session": payload.get("session_id"),
                                                "tool": tool_name, **result.to_dict()})
    if result.allowed:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": result.message()}}
