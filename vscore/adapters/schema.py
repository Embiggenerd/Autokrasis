"""Database tables from read-only catalog queries. Only statistics are read, never row contents.

SQLite is read with the standard library in read-only mode. PostgreSQL is read through the `psql`
client, so no driver is needed; it reports the planner's row estimates rather than exact counts.
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Table:
    database: str
    schema: str
    name: str
    rows: int
    references_in: int  # foreign keys in other tables that point here
    references_out: int
    personal_columns: tuple[str, ...]

    @property
    def node(self) -> str:
        return f"table:{self.database}.{self.schema}.{self.name}"


def _personal(columns: list[str], tokens: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(c for c in columns if any(t in c.lower() for t in tokens)))


def sqlite_path(url: str) -> Path | None:
    if url.startswith("sqlite:///"):
        return Path(url[len("sqlite:///") :])
    if url.endswith((".sqlite", ".sqlite3", ".db")) and "://" not in url:
        return Path(url)
    return None


def sqlite_tables(path: Path, personal_tokens: tuple[str, ...]) -> list[Table]:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        outgoing = {n: [r[2] for r in db.execute(f'PRAGMA foreign_key_list("{n}")')] for n in names}
        tables = []
        for name in names:
            columns = [r[1] for r in db.execute(f'PRAGMA table_info("{name}")')]
            rows = db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            incoming = sum(targets.count(name) for other, targets in outgoing.items() if other != name)
            tables.append(Table(path.stem, "main", name, rows, incoming, len(outgoing[name]),
                                _personal(columns, personal_tokens)))
        return tables
    finally:
        db.close()


_PG_QUERY = """
SELECT n.nspname, c.relname, GREATEST(c.reltuples, 0)::bigint,
  (SELECT count(*) FROM pg_constraint k WHERE k.contype = 'f' AND k.confrelid = c.oid),
  (SELECT count(*) FROM pg_constraint k WHERE k.contype = 'f' AND k.conrelid = c.oid),
  (SELECT string_agg(a.attname, ',') FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY 1, 2
"""


def postgres_tables(url: str, personal_tokens: tuple[str, ...]) -> list[Table]:
    out = subprocess.run(["psql", url, "-At", "-F", "\t", "-c", _PG_QUERY], capture_output=True, text=True, check=True)
    database = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    tables = []
    for line in out.stdout.splitlines():
        schema, name, rows, incoming, outgoing, columns = line.split("\t")
        tables.append(Table(database, schema, name, int(rows), int(incoming), int(outgoing),
                            _personal(columns.split(","), personal_tokens)))
    return tables


def read_tables(url: str, personal_tokens: tuple[str, ...], base: Path | None = None) -> list[Table]:
    """Tables behind a database URL; a relative SQLite path is taken relative to `base` (the repository)."""
    if (path := sqlite_path(url)) is not None:
        return sqlite_tables(path if path.is_absolute() or base is None else base / path, personal_tokens)
    if url.startswith(("postgres://", "postgresql://")):
        return postgres_tables(url, personal_tokens)
    raise ValueError(f"unsupported database URL {url!r}; use sqlite:///path or postgresql://...")
