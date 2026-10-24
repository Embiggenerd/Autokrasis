"""Agent integration: a Claude Code PreToolUse hook (plan: Enforcement layers, agent hooks).

The hook reads the tool call from stdin, prices it with the newest saved prices, and answers:

- allow: no output, so Claude Code's normal permission flow applies (the hook never grants permission)
- warn mode, or a reversible change within budget: a `systemMessage` with the price
- enforce mode, approval needed: permissionDecision "ask" with the reason
- enforce mode, blocked: permissionDecision "deny" with the reason, which Claude reads
- a database change that needs a snapshot: denied with the `vscore broker sql` command to use instead

Hooks are advisory: a shell can go around them. Resource permissions and the broker are the guarantee.
"""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from vscore.audit import AuditLog
from vscore.budget import load_report
from vscore.config import Config
from vscore.gate import Decision, evaluate, gate_log_path
from vscore.gitrepo import GitError, Repo
from vscore.prices.run import load_latest, price_repo, save_latest
from vscore.rules import rules_evaluate

MATCHER = "Bash|Write|Edit|MultiEdit|NotebookEdit"


def repo_root(cwd: str) -> str | None:
    try:
        return Repo(cwd).run("rev-parse", "--show-toplevel").decode().strip()
    except (GitError, FileNotFoundError, NotADirectoryError):
        return None


def load_config(root: str) -> Config:
    path = Path(root) / ".vscore" / "config.toml"
    cfg = Config.load(path if path.exists() else None)
    if mode := os.environ.get("VSCORE_GATE_MODE"):
        cfg = replace(cfg, gate_mode=mode)
    return cfg


def report_path(root: str) -> str:
    return os.environ.get("VSCORE_REPORT") or str(Path(root) / ".vscore" / "report.json")


def _broker_hint(decision: Decision, prices: dict | None) -> str:
    urls = (prices or {}).get("prices", {}).get("databases", [])
    statements = list(dict.fromkeys(i.target.detail for i in decision.price.items if i.target.node.startswith("table:")))
    if not urls or not statements:
        return ""
    sql = "; ".join(statements)
    return f" Run it through the broker, which snapshots first: vscore broker sql {shlex.quote(urls[0])} {shlex.quote(sql)}"


def respond(decision: Decision, prices: dict | None) -> dict | None:
    """The hook's JSON answer for a decision, or None to stay silent."""
    if decision.effective == "allow":
        return None
    message = decision.message()
    touches_tables = any(i.target.node.startswith("table:") for i in decision.price.items)
    brokerable = decision.via_broker is not None and decision.via_broker.verdict != "block"
    if decision.effective == "warn" or (decision.effective == "allow_after_snapshot" and not touches_tables):
        prefix = f"[warn mode: enforce would {decision.verdict.replace('_', ' ')}] " if decision.effective == "warn" else ""
        return {"systemMessage": prefix + message + (_broker_hint(decision, prices) if brokerable else "")}
    if decision.effective == "allow_after_snapshot" or brokerable:
        reason = message + _broker_hint(decision, prices)
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": reason}}
    permission = "ask" if decision.effective == "approve" else "deny"
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": permission,
                                   "permissionDecisionReason": message}}


def claude_code(payload: dict, policy: str = "vscore") -> dict | None:
    cwd = payload.get("cwd") or os.getcwd()
    root = repo_root(cwd)
    if root is None:
        return None
    cfg = load_config(root)
    prices = load_latest(root)
    if prices is None and policy == "vscore":
        result = price_repo(root, cfg=cfg)
        save_latest(root, result)
        prices = result.data
    tool_name, tool_input = payload.get("tool_name", ""), payload.get("tool_input", {}) or {}
    if policy == "rules":
        decision = rules_evaluate(tool_name, tool_input, root, cwd, prices, cfg.gate_mode)
    else:
        decision = evaluate(tool_name, tool_input, root, cwd, prices, cfg, load_report(report_path(root)))
    AuditLog(gate_log_path(Repo(root))).append({
        "at": datetime.now(UTC).isoformat(timespec="seconds"), "policy": policy,
        "session": payload.get("session_id"), **decision.to_dict(),
    })
    return respond(decision, prices)


def settings_snippet(policy: str = "vscore") -> str:
    command = "vscore hook claude-code" + (" --rules" if policy == "rules" else "")
    snippet = {"hooks": {"PreToolUse": [{"matcher": MATCHER, "hooks": [{"type": "command", "command": command}]}]}}
    return json.dumps(snippet, indent=2) + "\n"
