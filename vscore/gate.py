"""The gate: price an action, compare it with the evidence budget, decide (plan: The gate and enforcement).

| Action price        | Covered by budget? | Reversible?         | Decision                                   |
| Band 0              | any                | any                 | allow                                      |
| Band 1 or higher    | yes                | yes, tested restore | allow after a snapshot; show the price     |
| Band 1 or higher    | yes                | no                  | human approval; ask for a reversible plan  |
| any                 | no                 | any                 | block; show the price, ask for less        |

In warn mode every decision except allow is reported as `warn` with what enforce mode would have done.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from vscore.actions import DESTRUCTIVE, Action, Context, Target, from_tool_call
from vscore.adapters.history import info_bytes
from vscore.adapters.schema import sqlite_path
from vscore.budget import Budget, budget
from vscore.config import Config
from vscore.gitrepo import GitError, Repo, decode_path
from vscore.nodes import node_path
from vscore.prices.model import band

# Hours that stand for a band when nothing finer is known (geometric middles of each band).
BAND_HOURS = {0: 0.3, 1: 3.0, 2: 30.0, 3: 300.0, 4: 3000.0}
_TABLE_LOSS = {"drop", "db_reset", "truncate", "bulk_delete", "bulk_update", "drop_column", "unresolved"}


@dataclass(frozen=True)
class TargetPrice:
    target: Target
    hours: float
    band: int
    reversible: bool
    note: str


@dataclass(frozen=True)
class ActionPrice:
    hours: float
    band: int
    reversible: bool
    items: tuple[TargetPrice, ...] = ()


@dataclass(frozen=True)
class Decision:
    verdict: str  # what enforce mode decides: allow | allow_after_snapshot | approve | block
    effective: str  # what happens now: the verdict in enforce mode, `warn` for non-allow verdicts in warn mode
    mode: str
    action: Action
    price: ActionPrice
    budget: Budget
    reasons: tuple[str, ...] = field(default_factory=tuple)
    via_broker: Decision | None = None  # the same change run by the broker after a verified snapshot

    def message(self) -> str:
        text = "vscore: " + " ".join(self.reasons)
        if self.via_broker is not None and self.via_broker.verdict != "block":
            text += (f" Through the broker, with a verified snapshot first, it would cost {self.via_broker.price.hours:.1f} h"
                     f" and {self.via_broker.verdict.replace('_', ' ')}.")
        return text

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict, "effective": self.effective, "mode": self.mode, "tool": self.action.tool,
            "summary": self.action.summary, "hours": round(self.price.hours, 2), "band": self.price.band,
            "reversible": self.price.reversible, "budget_hours": round(self.budget.hours, 2),
            "targets": [{"node": i.target.node, "effect": i.target.effect, "hours": round(i.hours, 2), "band": i.band}
                        for i in self.price.items],
            "reasons": list(self.reasons),
        }


def build_context(repo_root: str, cwd: str, prices: dict | None) -> Context:
    repo = Repo(repo_root)
    tracked = {decode_path(p) for p in repo.run("ls-files", "-z").split(b"\0") if p}
    ctx = Context(repo_root=str(repo.path), cwd=cwd or str(repo.path), tracked=tracked)
    for node in (prices or {}).get("nodes", []):
        if node["node"].startswith("table:"):
            name = node["node"].rsplit(".", 1)[-1].lower()
            ctx.tables.setdefault(name, []).append(node["node"])
    for url in (prices or {}).get("prices", {}).get("databases", []):
        if (path := sqlite_path(url)) is not None:
            ctx.databases[os.path.realpath(os.path.join(ctx.repo_root, path))] = path.stem
    return ctx


def uncommitted_hours(repo: Repo, cfg: Config, paths: list[str] | None = None, untracked: bool = False) -> float:
    """Work an action would discard that Git cannot bring back: uncommitted edits, or untracked files."""
    if untracked:
        names = [decode_path(p) for p in repo.run("ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if p]
        total = 0
        for name in names:
            try:
                total += info_bytes((repo.path / name).read_bytes()[: cfg.max_file_bytes], b"", cfg.zstd_level)
            except OSError:
                continue
        return total / cfg.bytes_per_hour
    args = ["diff", "HEAD", "-w", "-U0", "--no-color", "--"] + (paths or [])
    try:
        diff = repo.run(*args)
    except GitError:
        return 0.0
    added = b"".join(line[1:] + b"\n" for line in diff.split(b"\n") if line.startswith(b"+") and not line.startswith(b"+++"))
    return info_bytes(added, b"", cfg.zstd_level) / cfg.bytes_per_hour if added else 0.0


def _dirty(repo: Repo) -> set[str]:
    out = repo.run("status", "--porcelain", "-z", "--untracked-files=no")
    return {decode_path(entry[3:]) for entry in out.split(b"\0") if len(entry) > 3}


def price_action(action: Action, prices: dict | None, ctx: Context, cfg: Config, snapshot: bool = False) -> ActionPrice:
    """Sums what each target would lose. With `snapshot`, the broker has just taken and verified a
    snapshot of the affected database, so table losses cost a restore instead of a rebuild."""
    by_node = {n["node"]: n for n in (prices or {}).get("nodes", [])}
    strongest: dict[str, Target] = {}
    for target in action.targets:
        current = strongest.get(target.node)
        rank = DESTRUCTIVE.index(target.effect) if target.effect in DESTRUCTIVE else len(DESTRUCTIVE)
        if current is None or rank < (DESTRUCTIVE.index(current.effect) if current.effect in DESTRUCTIVE else len(DESTRUCTIVE)):
            strongest[target.node] = target
    removed = {n[5:] for n, t in strongest.items() if n.startswith("file:") and t.effect in ("delete", "rewrite")}
    repo = Repo(ctx.repo_root)
    dirty = _dirty(repo) if any(n.startswith(("file:", "sym:")) for n in strongest) else set()

    items = []
    for node, target in sorted(strongest.items()):
        if node.startswith("sym:") and node_path(node) in removed:
            continue  # the whole file is going: its price already covers the symbol
        price = by_node.get(node)
        if target.effect in ("move", "rename_table"):
            items.append(TargetPrice(target, 0.0, 0, True, "reversible"))
        elif node.startswith(("file:", "sym:")) and node != "file:?":
            path = node_path(node)
            if price is None:
                items.append(TargetPrice(target, 0.0, 0, True, "not priced (excluded or new)"))
                continue
            hours = price["hours"] if price["hours"] is not None else BAND_HOURS[price["band"]]
            shared = len(set(price.get("importers", [])) & removed)  # their harm is counted once, not per file
            hours = max(hours - shared * cfg.harm_hours_per_dependent, 0.0)
            reversible = path not in dirty
            note = "restorable from Git" if reversible else "has uncommitted changes"
            if not reversible:
                hours += uncommitted_hours(repo, cfg, [path])
            item_band = band(hours) if price["hours"] is not None else price["band"]
            if target.effect == "unresolved":
                item_band, hours, note = max(3, item_band), max(hours, BAND_HOURS[3]), "unknown command"
            items.append(TargetPrice(target, hours, item_band, reversible and target.effect != "unresolved", note))
        elif node.startswith("table:"):
            if price is None:
                items.append(TargetPrice(target, BAND_HOURS[3], 3, False, "unpriced table: unknown is expensive"))
            elif target.effect == "partial_delete":
                items.append(TargetPrice(target, min(price["hours"], 2.0), band(min(price["hours"], 2.0)), False,
                                         "rows matching a WHERE clause"))
            elif target.effect in _TABLE_LOSS and snapshot:
                hours = cfg.table_restore_hours + price["parts"].get("harm", 0.0)
                items.append(TargetPrice(target, hours, band(hours), True, "broker snapshot verified"))
            elif target.effect in _TABLE_LOSS:
                tested = price["parts"].get("restore", 0) < price["parts"].get("rebuild", 0)
                hours = price["hours"] if tested else price["at_risk_hours"]
                note = "tested restore exists" if tested else "no tested restore"
                items.append(TargetPrice(target, hours, max(band(hours), price["band"]), tested, note))
        elif node in ("git:worktree", "git:untracked", "git:stash"):
            if node == "git:stash":
                hours = BAND_HOURS[1]
            else:
                paths = target.detail.split() if node == "git:worktree" and target.detail and not target.detail.startswith("git") else None
                hours = uncommitted_hours(repo, cfg, paths, untracked=node == "git:untracked")
            items.append(TargetPrice(target, hours, band(hours), False, "uncommitted work Git cannot restore"))
        else:  # git:history, file:? and other unknowns
            items.append(TargetPrice(target, BAND_HOURS[3], 3, False, "cannot be priced: unknown is expensive"))

    total = sum(i.hours for i in items)
    action_band = max([band(total)] + [i.band for i in items if i.target.effect == "unresolved" or i.band >= 3 and not i.reversible])
    return ActionPrice(total, action_band, all(i.reversible for i in items), tuple(items))


def _in_scope(target: Target, spend: Budget) -> bool:
    """A discard of named paths is in scope when every named file is."""
    if target.node == "git:worktree" and target.detail and not target.detail.startswith("git"):
        return all(spend.covers(f"file:{p}") for p in target.detail.split())
    return spend.covers(target.node)


def decide(action: Action, price: ActionPrice, spend: Budget, mode: str) -> Decision:
    top = sorted(price.items, key=lambda i: -i.hours)[:3]
    detail = "; ".join(f"{i.target.effect} {i.target.node} ({i.hours:.1f} h, {i.note})" for i in top)
    inside = sum(i.hours for i in price.items if _in_scope(i.target, spend))
    outside = price.hours - inside
    head = f"price {price.hours:.1f} h (band {price.band}) vs budget {spend.hours:.1f} h."
    if outside > 0:
        head += (f" {outside:.1f} h of it falls outside what the evidence is about, where only "
                 f"{spend.outside_hours:.1f} h may be spent.")
    covered = inside <= spend.hours and (outside == 0 or outside <= spend.outside_hours)
    if not price.items or price.band == 0:
        verdict, reasons = "allow", (f"price {price.hours:.1f} h (band 0).",) if price.items else ()
    elif covered and price.reversible:
        verdict = "allow_after_snapshot"
        reasons = (head, detail, "Covered and reversible: take a snapshot first.")
    elif covered:
        verdict = "approve"
        reasons = (head, detail, "Covered but irreversible: needs human approval; prefer a reversible change.")
    else:
        verdict = "block"
        reasons = (head, detail, "Not covered by the evidence: make a narrower change, or bring stronger evidence.")
    effective = verdict if mode == "enforce" or verdict == "allow" else "warn"
    return Decision(verdict, effective, mode, action, price, spend, reasons)


def evaluate(tool_name: str, tool_input: dict, repo_root: str, cwd: str, prices: dict | None, cfg: Config,
             report: dict | None) -> Decision:
    ctx = build_context(repo_root, cwd, prices)
    action = from_tool_call(tool_name, tool_input, ctx)
    price = price_action(action, prices, ctx, cfg)
    spend = budget(report, cfg)
    decision = decide(action, price, spend, cfg.gate_mode)
    if decision.verdict != "allow" and any(i.target.node.startswith("table:") and i.target.effect in _TABLE_LOSS
                                           for i in price.items):
        brokered = decide(action, price_action(action, prices, ctx, cfg, snapshot=True), spend, cfg.gate_mode)
        decision = replace(decision, via_broker=brokered)
    return decision


def gate_log_path(repo: Repo) -> Path:
    return repo.git_dir / "vscore-cache" / "gate.jsonl"
