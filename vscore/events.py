"""Append-only SQLite event store (plan: Event schema).

Every source is translated into one event format before anything is priced. Events are immutable
facts about history: paths are stored as they were at the time, and the mapping to today's files is
a view computed at scan time (see vscore.nodes.Lineage).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

SOURCES = ("structure", "schema", "git", "ci", "review", "issue", "incident", "agent_log", "telemetry")
KINDS = ("create", "modify", "delete", "rename", "review", "test_run", "report", "incident", "deploy", "traffic")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    source TEXT NOT NULL,
    rev TEXT,
    episode TEXT,
    actor_kind TEXT,
    actor_id TEXT,
    nodes TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    verified_by TEXT,
    content_hash TEXT NOT NULL,
    links TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS events_rev ON events(rev);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
    BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
    BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class Event:
    source: str
    kind: str
    at: str
    key: str  # natural key within the source, e.g. "<commit>:<path>"; makes re-ingestion idempotent
    nodes: tuple[str, ...] = ()
    payload: dict = field(default_factory=dict)
    rev: str | None = None
    episode: str | None = None
    actor_kind: str | None = None
    actor_id: str | None = None
    verified_by: str | None = None
    links: tuple[str, ...] = ()

    def __post_init__(self):
        if self.source not in SOURCES:
            raise ValueError(f"unknown event source {self.source!r}")
        if self.kind not in KINDS:
            raise ValueError(f"unknown event kind {self.kind!r}")

    @property
    def id(self) -> str:
        return hashlib.sha256(f"{self.source}\0{self.key}".encode()).hexdigest()[:32]

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(canonical(self.payload).encode()).hexdigest()[:32]


class ActorHasher:
    """Keyed hashes of author identities, so the store never holds names or emails (plan: Scoring people)."""

    def __init__(self, key_file: Path):
        key_file.parent.mkdir(parents=True, exist_ok=True)
        if not key_file.exists():
            tmp = key_file.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_bytes(os.urandom(32))
            tmp.replace(key_file)
        self._key = key_file.read_bytes()

    def __call__(self, identity: str) -> str:
        return hmac.new(self._key, identity.strip().lower().encode(), hashlib.sha256).hexdigest()[:16]


class EventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)

    @classmethod
    def for_repo(cls, git_dir: Path) -> EventStore:
        return cls(git_dir / "vscore-cache" / "events.sqlite")

    def add(self, events: Iterable[Event]) -> int:
        """Inserts events not already present; returns how many were new."""
        rows = [
            (e.id, e.at, e.source, e.rev, e.episode, e.actor_kind, e.actor_id, canonical(list(e.nodes)), e.kind,
             canonical(e.payload), e.verified_by, e.content_hash, canonical(list(e.links)))
            for e in events
        ]
        before = self.count()
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        return self.count() - before

    def count(self, source: str | None = None) -> int:
        if source is None:
            return self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return self.db.execute("SELECT COUNT(*) FROM events WHERE source = ?", (source,)).fetchone()[0]

    def query(self, source: str | None = None, kind: str | None = None) -> list[dict]:
        """Events as dicts, ordered by id so recomputation always sees the same order."""
        clauses, params = [], []
        if source:
            clauses.append("source = ?")
            params.append(source)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.execute(f"SELECT * FROM events {where} ORDER BY id", params).fetchall()
        return [
            {**dict(r), "nodes": json.loads(r["nodes"]), "payload": json.loads(r["payload"]),
             "links": json.loads(r["links"])}
            for r in rows
        ]

    def window(self, source: str) -> tuple[str, str] | None:
        """Earliest and latest event time for a source; None if it was never ingested."""
        row = self.db.execute("SELECT MIN(at), MAX(at) FROM events WHERE source = ?", (source,)).fetchone()
        return None if row[0] is None else (row[0], row[1])

    def close(self) -> None:
        self.db.close()
