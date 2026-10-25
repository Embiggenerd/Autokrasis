"""The gate: price an action, compare it with the evidence budget, decide (plan: The gate and enforcement).

| Action price        | Covered by budget? | Reversible?         | Decision                                   |
| Band 0              | any                | any                 | allow                                      |
| Band 1 or higher    | yes                | yes, tested restore | allow after a snapshot; show the price     |
| Band 1 or higher    | yes                | no                  | human approval; ask for a reversible plan  |
| any                 | no                 | any                 | block; show the price, ask for less        |

Below enforcement level 2 every decision except allow is reported as `warn` with what enforce mode would
have done; at level 0 nothing interrupts the agent. When the gate cannot decide normally, the failure
table of contract v1 (integration plan) decides, and the record names the row in `error`:

| Condition                                    | error              | Levels 0, 1 | Levels 2, 3                |
| No prices, not priced within the time limit  | prices_missing     | warn        | deny destructive-looking   |
| Prices from another commit, not repriced     | prices_stale       | warn        | deny destructive-looking   |
| A script that names priced data              | unsupported_action | allow       | ask                        |
| A tool vscore does not read                  | unsupported_action | allow       | allow                      |
| A malformed tool call                        | malformed_request  | warn        | deny                       |
| The core crashed, or missed the time limit   | internal_error,    | warn        | deny destructive-looking   |
|   (decided in hooks.py)                      | unavailable        |             |                            |
| The agent approving its own action           | self_approval      | warn        | deny                       |

"Deny destructive-looking" allows everything else. Every decision carries an id, the hash of its action and
of the state it touches, and, when it asks, an expiry: an approval binds to them (vscore/approvals.py).
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field, replace
from pathlib import Path

from vscore import FORMULA_VERSION
from vscore.actions import (DESTRUCTIVE, SELF_APPROVAL, TOOLS, UNSUPPORTED, Action, Context, Target,
                             destructive_looking, from_tool_call, malformed)
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
    harm: float = 0.0  # the part of `hours` that falls on the files importing the target
    harm_scope: tuple[str, ...] = ()  # those files' nodes


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
    revision: str = ""  # the commit the prices were computed at
    freshness: str = "fresh"  # `repriced (<cause>)`, or `stale (<cause>)` / `missing` when not repriced in time
    level: int = 1  # the enforcement level the decision was made at
    error: str | None = None  # the failure-table row that decided, if any
    failure_outcome: str | None = None  # the outcome that row requires
    decision_id: str = field(default_factory=lambda: secrets.token_hex(8))
    action_hash: str = ""  # the tool call, with repository-relative paths (approvals.action_hash)
    state_hash: str = ""  # the current state of everything the action touches (approvals.state_hash)
    expires: str | None = None  # for an ask: when it stops being approvable
    approval: dict | None = None  # the approval an executor redeemed for this decision: its decision id, who gave it
    @property
    def touches_tables(self) -> bool:
        return any(i.target.node.startswith("table:") for i in self.price.items)

    @property
    def outcome(self) -> str:
        """Contract v1's outcome: allow (runs silently), warn (runs, with the price shown), ask, or deny.
        A covered, reversible database change is denied with the broker route, which snapshots first."""
        if self.approval is not None:
            return "allow"
        if self.failure_outcome is not None:
            return self.failure_outcome
        if self.level == 0:
            return "allow"
        brokerable = self.via_broker is not None and self.via_broker.verdict != "block"
        if self.effective in ("allow", "warn"):
            return self.effective
        if self.effective == "allow_after_snapshot" and not self.touches_tables:
            return "warn"
        if self.effective == "allow_after_snapshot" or brokerable:
            return "deny"
        return "ask" if self.effective == "approve" else "deny"

    def message(self) -> str:
        text = "vscore: " + " ".join(self.reasons)
        if self.via_broker is not None and self.via_broker.verdict != "block":
            text += (f" Through the broker, with a verified snapshot first, it would cost {self.via_broker.price.hours:.1f} h"
                     f" and {self.via_broker.verdict.replace('_', ' ')}.")
        return text

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome, "verdict": self.verdict, "effective": self.effective, "mode": self.mode,
            "tool": self.action.tool,
            "summary": self.action.summary, "hours": round(self.price.hours, 2), "band": self.price.band,
            "reversible": self.price.reversible, "budget_hours": round(self.budget.hours, 2),
            "targets": [{"node": i.target.node, "effect": i.target.effect, "hours": round(i.hours, 2), "band": i.band}
                        for i in self.price.items],
            "reasons": list(self.reasons), "revision": self.revision, "freshness": self.freshness,
            "level": self.level, "error": self.error, "decision_id": self.decision_id,
            "action_hash": self.action_hash, "state_hash": self.state_hash, "expires": self.expires,
            "approval": self.approval,
        }


def current_prices(repo_root: str, prices: dict | None, cfg: Config, repo: Repo | None = None,
                   wait: float = 0.0) -> tuple[dict | None, str, str | None]:
    """Prices for the repository's current commit (integration plan, contract v1: Freshness), with their
    freshness and, when they could not be had in time, the failure-table code. Missing or stale prices
    start pricing in a detached process, keeping their databases and calibration; the caller waits up to
    `wait` seconds for it and otherwise decides with what it has."""
    from vscore.prices import background

    head = (repo or Repo(repo_root)).resolve("HEAD")
    meta = (prices or {}).get("prices", {})
    if prices is None:
        code, cause = "prices_missing", "prices_missing"
    elif meta.get("revision") != head:
        code, cause = "prices_stale", f"prices_stale: priced at {str(meta.get('revision'))[:12]}, HEAD is {head[:12]}"
    elif meta.get("formula") != FORMULA_VERSION:
        code, cause = "prices_stale", f"prices_stale: formula {meta.get('formula')}, current {FORMULA_VERSION}"
    else:
        return prices, "fresh", None
    databases = tuple(dict.fromkeys([*meta.get("databases", []), *cfg.databases]))
    process = background.start(repo_root, cfg, databases, meta.get("calibration"))
    if wait > 0 and (fresh := background.wait(repo_root, head, FORMULA_VERSION, wait, process)) is not None:
        return fresh, f"repriced ({cause})", None
    return prices, "missing" if prices is None else f"stale ({cause})", code


def build_context(repo_root: str, cwd: str, prices: dict | None, repo: Repo | None = None,
                  databases: tuple[str, ...] = ()) -> Context:
    repo = repo or Repo(repo_root)
    tracked = {decode_path(p) for p in repo.run("ls-files", "-z").split(b"\0") if p}
    ctx = Context(repo_root=str(repo.path), cwd=cwd or str(repo.path), tracked=tracked)
    for node in (prices or {}).get("nodes", []):
        if node["node"].startswith("table:"):
            name = node["node"].rsplit(".", 1)[-1].lower()
            ctx.tables.setdefault(name, []).append(node["node"])
    for url in [*(prices or {}).get("prices", {}).get("databases", []), *databases]:
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


def price_action(action: Action, prices: dict | None, ctx: Context, cfg: Config, snapshot: bool = False,
                 repo: Repo | None = None) -> ActionPrice:
    """Sums what each target would lose. With `snapshot`, the broker has just taken and verified a
    snapshot of the affected database, so table losses cost a restore instead of a rebuild."""
    by_node = {n["node"]: n for n in (prices or {}).get("nodes", [])}
    strongest: dict[str, Target] = {}
    for target in action.targets:
        current = strongest.get(target.node)
        rank = DESTRUCTIVE.index(target.effect) if target.effect in DESTRUCTIVE else len(DESTRUCTIVE)
        if current is None or rank < (DESTRUCTIVE.index(current.effect) if current.effect in DESTRUCTIVE else len(DESTRUCTIVE)):
            strongest[target.node] = target
    deleted = {n[5:] for n, t in strongest.items() if n.startswith("file:") and t.effect == "delete"}
    rewritten = {n[5:] for n, t in strongest.items() if n.startswith("file:") and t.effect in ("rewrite", "overwrite")}
    repo = repo or Repo(ctx.repo_root)
    dirty = _dirty(repo) if any(n.startswith(("file:", "sym:")) for n in strongest) else set()

    items = []
    for node, target in sorted(strongest.items()):
        if target.effect in (UNSUPPORTED, SELF_APPROVAL):
            continue  # nothing to price: a rule decides (evaluate)
        if node.startswith("sym:") and node_path(node) in deleted:
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
            importers = (by_node.get(f"file:{path}") or {}).get("importers", [])
            shared = len(set(importers) & deleted)  # their harm is counted once, not per file
            hours = max(hours - shared * cfg.harm_hours_per_dependent, 0.0)
            harm = max(((price.get("parts") or {}).get("harm") or 0.0) - shared * cfg.harm_hours_per_dependent, 0.0)
            if node.startswith("file:") and path in rewritten:
                # The file stays: its importers break only through the names it loses, priced as symbols.
                hours, harm = max(hours - harm, 0.0), 0.0
            scope = tuple(f"file:{p}" for p in importers if p not in deleted)
            reversible = path not in dirty
            note = "restorable from Git" if reversible else "has uncommitted changes"
            if not reversible:
                hours += uncommitted_hours(repo, cfg, [path])
            item_band = band(hours) if price["hours"] is not None else price["band"]
            if target.effect == "unresolved":
                item_band, hours, note = max(3, item_band), max(hours, BAND_HOURS[3]), "unknown command"
            items.append(TargetPrice(target, hours, item_band, reversible and target.effect != "unresolved", note,
                                     min(harm, hours), scope))
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


def _inside(item: TargetPrice, spend: Budget) -> float:
    """The hours of an item the evidence covers. Breaking the files that import a target spends outside
    the evidence's scope unless the evidence names those files too."""
    if not _in_scope(item.target, spend):
        return 0.0
    if item.harm and not all(spend.covers(node) for node in item.harm_scope):
        return item.hours - item.harm
    return item.hours


def decide(action: Action, price: ActionPrice, spend: Budget, level: int | str) -> Decision:
    """The decision table. `level` is the enforcement level, or a gate mode (`warn` is 1, `enforce` is 2)."""
    if isinstance(level, str):
        level = 2 if level == "enforce" else 1
    top = sorted(price.items, key=lambda i: -i.hours)[:3]
    detail = "; ".join(f"{i.target.effect} {i.target.node} ({i.hours:.1f} h, {i.note})" for i in top)
    inside = sum(_inside(i, spend) for i in price.items)
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
    effective = verdict if level >= 2 or verdict == "allow" else "warn"
    mode = {0: "advice", 1: "warn"}.get(level, "enforce")
    return Decision(verdict, effective, mode, action, price, spend, reasons, level=level)


def failure(code: str, outcome: str, reason: str, action: Action, spend: Budget, level: int) -> Decision:
    """A decision made by the failure table alone, without a price."""
    verdict = "block" if outcome in ("deny", "ask") else "allow"
    return Decision(verdict, verdict if level >= 2 else ("warn" if verdict != "allow" else "allow"),
                    {0: "advice", 1: "warn"}.get(level, "enforce"), action, ActionPrice(0.0, 0, True), spend,
                    (reason,), level=level, error=code, failure_outcome=outcome)


def bind(decision: Decision, digest: str, ctx: Context, repo: Repo, cfg: Config) -> Decision:
    """Adds what an approval binds to: the action's hash, the state's hash, and an expiry for an ask."""
    from vscore.approvals import expiry, state_hash

    return replace(decision, action_hash=digest, state_hash=state_hash(decision.action, ctx, repo),
                   expires=expiry(cfg.approval_ttl_s) if decision.outcome == "ask" else None)


def evaluate(tool_name: str, tool_input: dict, repo_root: str, cwd: str, prices: dict | None, cfg: Config,
             report: dict | None) -> Decision:
    """Decides one tool call. `prices` are the saved prices, if any; stale or missing ones are recomputed,
    within the time limit at levels 2 and 3."""
    from vscore.approvals import action_hash

    level = cfg.enforcement_level
    spend = budget(report, cfg)
    repo = Repo(repo_root)
    if (problem := malformed(tool_name, tool_input)) is not None:
        action = Action(tool_name if isinstance(tool_name, str) else "?", "malformed request")
        decision = failure("malformed_request", "deny" if level >= 2 else "warn",
                           f"Malformed request: {problem} (failure table: malformed_request).", action, spend, level)
        ctx = Context(repo_root=str(repo.path), cwd=cwd or str(repo.path))
        return bind(decision, action_hash(str(tool_name), tool_input, ctx), ctx, repo, cfg)
    decision, ctx = _decide(tool_name, tool_input, repo, cwd, prices, cfg, spend, level)
    return bind(decision, action_hash(tool_name, tool_input, ctx), ctx, repo, cfg)


def _decide(tool_name: str, tool_input: dict, repo: Repo, cwd: str, prices: dict | None, cfg: Config, spend: Budget,
            level: int) -> tuple[Decision, Context]:
    repo_root = str(repo.path)
    prices, freshness, error = current_prices(repo_root, prices, cfg, repo, cfg.time_limit_s if level >= 2 else 0.0)
    ctx = build_context(repo_root, cwd, prices, repo, cfg.databases)
    action = from_tool_call(tool_name, tool_input, ctx)
    price = price_action(action, prices, ctx, cfg, repo=repo)
    decision = decide(action, price, spend, level)
    if decision.verdict != "allow" and any(i.target.node.startswith("table:") and i.target.effect in _TABLE_LOSS
                                           for i in price.items):
        snapshotted = price_action(action, prices, ctx, cfg, snapshot=True, repo=repo)
        decision = replace(decision, via_broker=decide(action, snapshotted, spend, level))
    decision = replace(decision, revision=(prices or {}).get("prices", {}).get("revision", ""), freshness=freshness)
    return _rules(decision, action, tool_name, freshness, error, cfg, level), ctx


def _rules(decision: Decision, action: Action, tool_name: str, freshness: str, error: str | None, cfg: Config,
           level: int) -> Decision:
    """The rules that override the decision table: self-approval, then the failure table."""
    if any(t.effect == SELF_APPROVAL for t in action.targets):
        outcome = "deny" if level >= 2 else "warn"
        return replace(decision, error="self_approval", failure_outcome=outcome,
                       reasons=("Approvals come from a person, not the agent: this grants one or writes vscore's "
                                "own state under .git/vscore-cache (self_approval).",))
    if error is not None:  # prices missing or stale, and not repriced in time
        if level <= 1:
            outcome, why = "warn", "allowed with a warning; pricing has started in the background"
        elif destructive_looking(action):
            outcome, why = "deny", (f"pricing did not finish within {cfg.time_limit_s:g} s, so destructive-looking"
                                    " actions are denied until it does; it continues in the background, so try"
                                    " again shortly")
        else:
            outcome, why = "allow", "allowed: nothing here looks destructive"
        return replace(decision, error=error, failure_outcome=outcome,
                       reasons=decision.reasons + (f"Prices are {freshness}: {why} (failure table: {error}).",))
    if tool_name not in TOOLS:
        return replace(decision, error="unsupported_action", failure_outcome="allow",
                       reasons=(f"vscore does not read the {tool_name} tool: allowed and logged "
                                "(failure table: unsupported_action).",))
    if any(t.effect == UNSUPPORTED for t in action.targets):
        current = decision.outcome
        if level >= 2:
            outcome = current if current == "deny" else "ask"
        else:
            outcome = current if current == "warn" else "allow"
        note = next(n for n in action.notes if "runs a script" in n)
        return replace(decision, error="unsupported_action", failure_outcome=outcome,
                       reasons=decision.reasons + (f"{note}: it names priced data, so it "
                                                   f"{'needs approval' if outcome == 'ask' else 'is logged'} "
                                                   "(failure table: unsupported_action).",))
    return decision


def gate_log_path(repo: Repo) -> Path:
    return repo.git_dir / "vscore-cache" / "gate.jsonl"
