"""Prices for a repository: scan, ingest Git history, gather evidence, price files and tables."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from vscore import FORMULA_VERSION, __version__
from vscore.adapters.gitlog import git_events
from vscore.adapters.schema import read_tables
from vscore.config import Config
from vscore.events import ActorHasher, EventStore
from vscore.evidence import Evidence, gather
from vscore.gitrepo import Repo
from vscore.nodes import Lineage
from vscore.prices.model import Price, price_file, price_symbol, price_table
from vscore.scan import ScanResult, scan


@dataclass
class PriceResult:
    data: dict
    scan: ScanResult
    prices: dict[str, Price]
    evidence: Evidence

    def to_json(self) -> str:
        return json.dumps(self.data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def cache_dir(repo: Repo) -> Path:
    return repo.git_dir / "vscore-cache"


def price_repo(repo_path, rev: str = "HEAD", cfg: Config | None = None, databases: tuple[str, ...] = (),
               calibration: dict | None = None, jobs: int | None = None, now: str | None = None,
               allow_shallow: bool = False) -> PriceResult:
    cfg = cfg or Config()
    now = now or datetime.now(UTC).isoformat(timespec="seconds")
    result = scan(repo_path, rev, cfg, allow_shallow=allow_shallow, jobs=jobs)
    repo = Repo(repo_path)
    revision = result.data["scan"]["revision"]
    store = EventStore.for_repo(repo.git_dir)
    try:
        store.add(git_events(repo, revision, ActorHasher(cache_dir(repo) / "actor.key")))
        files = result.data["files"]
        lineage = Lineage.from_git(repo, revision, {f["path"] for f in files})
        evidence = gather(store, lineage)
        sources = {s: store.count(s) for s in ("git", "ci", "review", "incident", "telemetry", "schema")}
    finally:
        store.close()

    prices: dict[str, Price] = {}
    for record in files:
        found = result.contributions.get(record["path"])
        contributions = None if found is None else [(c.commit, c.work) for c in found]
        price = price_file(record["path"], record["class"], record["W"], record["D"], record["confidence"],
                           contributions, evidence, cfg, calibration)
        prices[price.node] = price
    file_classes = {f["path"]: f["class"] for f in files}
    for symbol in result.data.get("symbols", []):
        price = price_symbol(symbol, file_classes[symbol["path"]], cfg)
        prices[price.node] = price
    for url in databases:
        for table in read_tables(url, cfg.personal_columns, repo.path):
            price = price_table(table, evidence, cfg, now)
            prices[price.node] = price

    classes = {f["path"]: f["class"] for f in files}
    nodes = []
    for node in sorted(prices):
        entry = prices[node].to_dict()
        if node.startswith("file:"):
            entry["importers"] = [p for p in result.importers.get(node[5:], []) if classes.get(p) != "test"]
        nodes.append(entry)
    data = {
        "prices": {
            "revision": revision,
            "formula": FORMULA_VERSION,
            "scorer": __version__,
            "config_hash": cfg.digest(),
            "calibrated": calibration is not None,
            "calibration": calibration,
            "databases": list(databases),
            "events": sources,
            "verification_windows": evidence.windows,
        },
        "nodes": nodes,
    }
    return PriceResult(data, result, prices, evidence)


def save_latest(repo_path, result: PriceResult) -> Path:
    """Keeps the newest prices where the gate and hooks look for them."""
    target = cache_dir(Repo(repo_path)) / "prices" / "latest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(result.to_json())
    tmp.replace(target)
    return target


def load_latest(repo_path) -> dict | None:
    target = cache_dir(Repo(repo_path)) / "prices" / "latest.json"
    return json.loads(target.read_text()) if target.exists() else None
