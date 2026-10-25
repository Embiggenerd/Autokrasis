"""Pricing in a detached process (integration plan, contract v1: Failure table).

When the saved prices are missing or stale, the gate starts pricing here and either waits up to its
time limit (levels 2 and 3) or decides at once (levels 0 and 1). The process keeps running after the
caller exits and saves its result where the gate looks, so a later decision finds fresh prices. One
pricing process runs per repository at a time.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, fields
from pathlib import Path

from vscore.config import Config
from vscore.gitrepo import Repo
from vscore.prices.run import cache_dir, load_latest, price_repo, save_latest


def _running(lock: Path) -> int | None:
    """The pid of a live pricing process that holds the lock, if any."""
    try:
        pid = int(lock.read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def start(repo_root: str, cfg: Config, databases: tuple[str, ...], calibration: dict | None) -> subprocess.Popen | None:
    """Starts pricing the repository unless a pricing process is already at work; returns the process
    started, or None."""
    folder = cache_dir(Repo(repo_root)) / "prices"
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / "pricing.pid"
    if _running(lock) is not None:
        return None
    job = folder / "pricing-job.json"
    job.write_text(json.dumps({"repo": str(repo_root), "config": asdict(cfg), "databases": list(databases),
                               "calibration": calibration}))
    with open(folder / "pricing.log", "ab") as log:
        process = subprocess.Popen([sys.executable, "-m", "vscore.prices.background", str(job)],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    lock.write_text(str(process.pid))
    return process


def wait(repo_root: str, revision: str, formula: str, seconds: float,
         process: subprocess.Popen | None = None) -> dict | None:
    """Prices for `revision`, if pricing produces them within `seconds`."""
    deadline = time.monotonic() + seconds
    while True:
        latest = load_latest(repo_root)
        meta = (latest or {}).get("prices", {})
        if meta.get("revision") == revision and meta.get("formula") == formula:
            return latest
        remaining = deadline - time.monotonic()
        if remaining <= 0 or (process is not None and process.poll() is not None):
            return None
        time.sleep(min(0.05, remaining))


def main(argv: list[str]) -> int:
    job = json.loads(Path(argv[0]).read_text())
    known = {f.name for f in fields(Config)}
    values = {k: tuple(v) if isinstance(v, list) else v for k, v in job["config"].items() if k in known}
    result = price_repo(job["repo"], cfg=Config(**values), databases=tuple(job["databases"]),
                        calibration=job["calibration"])
    save_latest(job["repo"], result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
