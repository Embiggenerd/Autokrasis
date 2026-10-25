"""The broker: the only holder of rights to destroy priced data (plan: Enforcement layers).

It snapshots the database, verifies the snapshot, prices the change as restorable, asks the gate in
enforce mode, and only then runs the statement, logging every step. SQLite is handled with the
standard library; PostgreSQL uses `pg_dump` and `psql` when they are installed.

`verify_restore` records a tested restore as an event, which lowers a table's price for the next week.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from vscore.actions import Action, classify_sql
from vscore.adapters.schema import read_tables, sqlite_path
from vscore.approvals import Approvals, expiry, state_hash
from vscore.audit import AuditLog
from vscore.budget import budget
from vscore.config import Config
from vscore.events import Event, EventStore, canonical
from vscore.gate import build_context, decide, price_action
from vscore.gitrepo import Repo


class BrokerRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class Snapshot:
    path: Path
    rows: dict[str, int]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _counts(db: sqlite3.Connection) -> dict[str, int]:
    names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {n: db.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in sorted(names)}


class Broker:
    def __init__(self, repo_path, cfg: Config | None = None):
        self.repo = Repo(repo_path)
        self.cfg = cfg or Config()
        self.snapshots = self.repo.git_dir / "vscore-cache" / "snapshots"
        self.snapshots.mkdir(parents=True, exist_ok=True)
        self.log = AuditLog(self.repo.git_dir / "vscore-cache" / "broker.jsonl")
        self.approvals = Approvals(self.repo)

    def _db_path(self, url: str) -> Path:
        path = sqlite_path(url)
        if path is None:
            raise BrokerRefused(f"not a SQLite URL: {url}")
        return path if path.is_absolute() else self.repo.path / path

    def snapshot(self, url: str) -> Snapshot:
        """Copies the database with SQLite's backup API and checks the copy matches, table by table."""
        if url.startswith(("postgres://", "postgresql://")):
            return self._pg_snapshot(url)
        source_path = self._db_path(url)
        target = self.snapshots / f"{source_path.stem}-{datetime.now(UTC):%Y%m%dT%H%M%S}-{secrets.token_hex(3)}.sqlite"
        source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        copy = sqlite3.connect(target)
        try:
            source.backup(copy)
            if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BrokerRefused("snapshot failed its integrity check")
            rows = _counts(copy)
            if rows != _counts(source):
                raise BrokerRefused("snapshot row counts differ from the database")
        finally:
            source.close()
            copy.close()
        return Snapshot(target, rows)

    def _pg_snapshot(self, url: str) -> Snapshot:
        if not shutil.which("pg_dump") or not shutil.which("pg_restore"):
            raise BrokerRefused("pg_dump and pg_restore are needed to snapshot PostgreSQL")
        target = self.snapshots / f"pg-{datetime.now(UTC):%Y%m%dT%H%M%S}-{secrets.token_hex(3)}.dump"
        subprocess.run(["pg_dump", "-Fc", "-f", str(target), url], check=True, capture_output=True)
        subprocess.run(["pg_restore", "--list", str(target)], check=True, capture_output=True)
        return Snapshot(target, {})

    def verify_restore(self, url: str) -> Snapshot:
        """Snapshots, restores the snapshot into a scratch copy, compares rows, and records a tested restore."""
        snap = self.snapshot(url)
        if snap.path.suffix != ".sqlite":
            raise BrokerRefused("restore tests are implemented for SQLite only")
        with tempfile.TemporaryDirectory() as tmp:
            restored = Path(tmp) / "restored.sqlite"
            shutil.copyfile(snap.path, restored)
            check = sqlite3.connect(restored)
            try:
                if _counts(check) != snap.rows:
                    raise BrokerRefused("restored copy does not match the snapshot")
            finally:
                check.close()
        at = _now()
        tables = {t.name: t for t in read_tables(url, self.cfg.personal_columns, self.repo.path)}
        store = EventStore.for_repo(self.repo.git_dir)
        store.add(Event(source="schema", kind="test_run", at=at, key=f"restore:{snap.path.name}:{name}",
                        nodes=(tables[name].node,), payload={"rows": rows, "snapshot": snap.path.name},
                        verified_by=f"restore:{snap.path.name}") for name, rows in snap.rows.items() if name in tables)
        store.close()
        self.log.append({"at": at, "op": "verify_restore", "database": url, "snapshot": str(snap.path),
                         "tables": snap.rows})
        return snap

    def run_sql(self, url: str, sql: str, prices: dict | None, report: dict | None = None) -> dict:
        """Prices the statement as restorable from a fresh snapshot, asks the gate, and runs it if allowed.
        An irreversible change runs only on a person's approval of this statement and the database's
        current state (vscore/approvals.py), once."""
        ctx = build_context(str(self.repo.path), str(self.repo.path), prices)
        database = Path(url).stem if sqlite_path(url) else url.rstrip("/").rsplit("/", 1)[-1]
        if sqlite_path(url) is not None:
            ctx.databases.setdefault(os.path.realpath(self._db_path(url)), database)
        action = Action("broker", sql[:200], tuple(classify_sql(sql, ctx, database)))
        state = state_hash(action, ctx, self.repo)  # before the snapshot: what a person would be approving
        snap = self.snapshot(url) if action.targets else None
        price = price_action(action, prices, ctx, self.cfg, snapshot=snap is not None)
        decision = decide(action, price, budget(report, self.cfg), "enforce")
        digest = hashlib.sha256(canonical({"tool": "broker", "database": url, "sql": sql}).encode()).hexdigest()
        decision = replace(decision, action_hash=digest, state_hash=state,
                           expires=expiry(self.cfg.approval_ttl_s) if decision.verdict == "approve" else None)
        grant = None
        if decision.verdict == "approve":
            grant, problem = self.approvals.redeem(digest, state)
            if grant is not None:
                decision = replace(decision, approval={"decision_id": grant["decision_id"], "by": grant["by"]})
        entry = {"at": _now(), "op": "sql", "database": url, "sql": sql, "decision": decision.to_dict(),
                 "snapshot": str(snap.path) if snap else None}
        if decision.verdict == "block" or (decision.verdict == "approve" and grant is None):
            entry["result"] = "refused"
            self.log.append(entry)
            hint = ""
            if decision.verdict == "approve":
                self.approvals.request(decision.to_dict())
                hint = ((f" ({problem}.)" if problem else "") + " It needs a person's approval, once: vscore approve "
                        f"{shlex.quote(str(self.repo.path))} {decision.decision_id}")
            raise BrokerRefused(decision.message() + hint)
        try:
            self._execute(url, sql)
            entry["result"] = "ok"
        except Exception as exc:  # noqa: BLE001 - logged and re-raised
            entry["result"] = f"error: {exc}"
            self.log.append(entry)
            raise
        self.log.append(entry)
        return entry

    def _execute(self, url: str, sql: str) -> None:
        if url.startswith(("postgres://", "postgresql://")):
            subprocess.run(["psql", url, "-v", "ON_ERROR_STOP=1", "-1", "-c", sql], check=True, capture_output=True)
            return
        db = sqlite3.connect(self._db_path(url))
        try:
            db.executescript(f"BEGIN; {sql}; COMMIT;")
        except sqlite3.Error:
            db.rollback()
            raise
        finally:
            db.close()

    def restore(self, snapshot: str | Path, url: str) -> None:
        """Writes a SQLite snapshot back over the database."""
        target = sqlite3.connect(self._db_path(url))
        source = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()
        self.log.append({"at": _now(), "op": "restore", "database": url, "snapshot": str(snapshot)})


def postgres_agent_role_sql(role: str, prices: dict) -> str:
    """SQL that gives an agent's PostgreSQL role read and row-level write access but no DROP or TRUNCATE,
    and no DELETE on band-3 and band-4 tables; the broker keeps the owner credentials."""
    lines = [f"CREATE ROLE {role} LOGIN;", f"GRANT USAGE ON SCHEMA public TO {role};"]
    for node in prices.get("nodes", []):
        if not node["node"].startswith("table:"):
            continue
        _, schema, name = node["node"][6:].split(".", 2)
        table = f'"{schema}"."{name}"' if schema != "main" else f'"{name}"'
        privileges = "SELECT, INSERT, UPDATE" + ("" if node["band"] >= 3 else ", DELETE")
        lines.append(f"GRANT {privileges} ON {table} TO {role};  -- band {node['band']}")
    return "\n".join(lines) + "\n"
