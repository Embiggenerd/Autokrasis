"""Append-only, hash-chained JSON-lines log of vscore compare's checks.

Each entry carries the hash of the one before it, so editing or deleting an entry breaks the chain
and `verify` notices.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
from pathlib import Path


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def append(self, record: dict) -> dict:
        with open(self.path, "r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            lines = handle.read().splitlines()
            previous = json.loads(lines[-1])["hash"] if lines else "0" * 64
            entry = {**record, "prev": previous}
            entry["hash"] = hashlib.sha256(canonical(entry).encode()).hexdigest()
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry

    def entries(self) -> list[dict]:
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def verify(self) -> bool:
        previous = "0" * 64
        for entry in self.entries():
            body = {k: v for k, v in entry.items() if k != "hash"}
            if entry.get("prev") != previous or hashlib.sha256(canonical(body).encode()).hexdigest() != entry["hash"]:
                return False
            previous = entry["hash"]
        return True
