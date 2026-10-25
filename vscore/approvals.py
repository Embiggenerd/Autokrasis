"""Approvals bound to one decision (integration plan, contract v1: Approvals).

A decision that asks carries an id, the hash of its action and the hash of the current state of
everything the action touches, and an expiry. The executor (the Claude Code hook, the broker) files a
request for it; a person grants it with `vscore approve <repo> <decision id>`. Before running, the
executor recomputes both hashes and redeems a granted approval only if they match and it has not
expired. Redeeming moves the approval aside atomically, so one approval allows one execution.

State hashes cover file contents, a SQLite table's schema and row count, and for Git targets the
commit and the working tree's status. PostgreSQL tables are not hashed yet: their state is recorded as
unverifiable, so an approval for them binds to the action alone.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from vscore.actions import Action, Context
from vscore.events import canonical
from vscore.gitrepo import Repo
from vscore.nodes import node_path


class ApprovalError(RuntimeError):
    pass


def now() -> datetime:
    return datetime.now(UTC)


def expiry(seconds: float, at: datetime | None = None) -> str:
    return ((at or now()) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def expired(stamp: str, at: datetime | None = None) -> bool:
    return datetime.fromisoformat(stamp) <= (at or now())


def action_hash(tool_name: str, tool_input: object, ctx: Context) -> str:
    """The tool call with paths relative to the repository, so the same call hashes the same anywhere."""
    def relative(value):
        if isinstance(value, dict):
            return {k: relative(v) if k not in ("file_path", "notebook_path") else (ctx.rel(v) or v)
                    for k, v in value.items()}
        return value

    body = {"tool": tool_name, "input": relative(tool_input), "cwd": ctx.rel(ctx.cwd) or ctx.cwd}
    return hashlib.sha256(canonical(body).encode()).hexdigest()


def _file_state(path: str) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return "absent"


def _sqlite_state(path: str) -> list | str:
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return "unreadable"
    try:
        tables = db.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                            "ORDER BY name").fetchall()
        return [[name, sql, db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]] for name, sql in tables]
    except sqlite3.Error:
        return "unreadable"
    finally:
        db.close()


def state_hash(action: Action, ctx: Context, repo: Repo) -> str:
    """Hash of the current state of everything the action touches."""
    parts: dict[str, object] = {}

    def databases(name: str | None = None) -> None:
        for path, db in sorted(ctx.databases.items()):
            if name in (None, "?", db):
                parts[f"db:{ctx.rel(path) or path}"] = _sqlite_state(path)

    for target in action.targets:
        node = target.node
        if node.startswith(("file:", "sym:")) and node != "file:?":
            rel = node_path(node)
            parts[f"file:{rel}"] = _file_state(os.path.join(ctx.repo_root, rel))
        elif node.startswith("table:"):
            name = node[6:].split(".", 1)[0]
            if any(db == name for db in ctx.databases.values()) or name == "?":
                databases(name)
            else:
                parts[node] = "unverifiable"
        else:  # Git state, an unreadable action, vscore's own state
            parts["git"] = [repo.run("rev-parse", "HEAD").decode().strip(),
                            repo.run("status", "--porcelain", "-z").decode(errors="replace")]
            if node in ("action:?", "file:?"):
                databases()
    return hashlib.sha256(canonical(parts).encode()).hexdigest()


class Approvals:
    """Requests, grants and used approvals, one JSON file each, under .git/vscore-cache/approvals."""

    def __init__(self, repo: Repo):
        self.root = repo.git_dir / "vscore-cache" / "approvals"
        for folder in ("requests", "granted", "used"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)

    def _write(self, folder: str, record: dict) -> None:
        target = self.root / folder / f"{record['decision_id']}.json"
        tmp = target.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(record, sort_keys=True, indent=1))
        tmp.replace(target)

    def request(self, record: dict) -> None:
        """Files an ask so a person can approve it by its decision id."""
        keep = ("decision_id", "action_hash", "state_hash", "expires", "tool", "summary", "hours", "band", "reasons")
        self._write("requests", {k: record[k] for k in keep})

    def pending(self) -> list[dict]:
        found = [json.loads(p.read_text()) for p in sorted((self.root / "requests").glob("*.json"))]
        decided = {p.stem for folder in ("granted", "used") for p in (self.root / folder).glob("*.json")}
        return [r for r in found if not expired(r["expires"]) and r["decision_id"] not in decided]

    def approve(self, decision_id: str, by: str) -> dict:
        source = self.root / "requests" / f"{decision_id}.json"
        if not source.exists():
            raise ApprovalError(f"no open ask with decision id {decision_id}")
        request = json.loads(source.read_text())
        if any((self.root / f / f"{decision_id}.json").exists() for f in ("granted", "used")):
            raise ApprovalError(f"decision {decision_id} is already approved")
        if expired(request["expires"]):
            raise ApprovalError(f"decision {decision_id} expired at {request['expires']}; ask again")
        grant = {**request, "by": by, "approved_at": now().isoformat(timespec="seconds")}
        self._write("granted", grant)
        return grant

    def redeem(self, action: str, state: str) -> tuple[dict | None, str | None]:
        """A granted approval for this action and state, consumed; or None and why none applies."""
        problem = None
        for path in sorted((self.root / "granted").glob("*.json")):
            grant = json.loads(path.read_text())
            if grant["action_hash"] != action:
                continue
            if expired(grant["expires"]):
                problem = f"approval {grant['decision_id']} expired at {grant['expires']}"
                continue
            if grant["state_hash"] != state:
                problem = (f"approval {grant['decision_id']} no longer applies: what the action touches changed "
                           "since it was given")
                continue
            try:
                os.rename(path, self.root / "used" / path.name)  # atomic: only one execution wins
            except FileNotFoundError:
                continue
            return grant, None
        return None, problem
