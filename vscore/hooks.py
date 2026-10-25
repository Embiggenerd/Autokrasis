"""Agent integration: a Claude Code PreToolUse hook (plan: Enforcement layers, agent hooks).

The hook reads the tool call from stdin, prices it with prices for the current commit (the saved ones
when they match it, recomputed when they do not), and answers:

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
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from vscore.audit import AuditLog
from vscore.actions import Action, destructive_looking, from_tool_call
from vscore.approvals import Approvals
from vscore.budget import budget, load_report
from vscore.config import Config
from vscore.gate import Decision, build_context, evaluate, failure, gate_log_path
from vscore.gitrepo import GitError, Repo
from vscore.prices.run import load_latest
from vscore.rules import rules_evaluate

MATCHER = "Bash|Write|Edit|MultiEdit|NotebookEdit"


def repo_root(cwd: str) -> str | None:
    try:
        return Repo(cwd).run("rev-parse", "--show-toplevel").decode().strip()
    except (GitError, FileNotFoundError, NotADirectoryError):
        return None


def load_config(root: str) -> Config:
    """`.vscore/config.toml` (or the file VSCORE_CONFIG names), then VSCORE_GATE_MODE and VSCORE_LEVEL."""
    path = Path(os.environ.get("VSCORE_CONFIG") or Path(root) / ".vscore" / "config.toml")
    cfg = Config.load(path if path.exists() else None)
    if mode := os.environ.get("VSCORE_GATE_MODE"):
        cfg = replace(cfg, gate_mode=mode)
    if level := os.environ.get("VSCORE_LEVEL"):
        cfg = replace(cfg, level=int(level))
    return cfg


GRACE_S = 1.0  # beyond the core's own time limit, for reading the repository and pricing the action


def _within(seconds: float, fn):
    """fn(), or TimeoutError if it takes longer; a late result is abandoned, so the agent never hangs."""
    result: dict = {}

    def run():
        try:
            result["value"] = fn()
        except Exception as exc:  # re-raised in the caller
            result["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise TimeoutError(f"no decision within {seconds:g} s")
    if "error" in result:
        raise result["error"]
    return result["value"]


def fault(code: str, detail: str, tool_name, tool_input, root: str, cwd: str, cfg: Config,
          report: dict | None) -> Decision:
    """The failure table for a core that crashed (internal_error) or missed the time limit (unavailable):
    warn at levels 0 and 1; at levels 2 and 3 deny destructive-looking actions, which the parser alone
    decides, and allow the rest. An action even the parser cannot read counts as destructive-looking."""
    level = cfg.enforcement_level
    try:
        ctx = build_context(root, cwd, load_latest(root), databases=cfg.databases)
        action = from_tool_call(tool_name, tool_input, ctx)
        destructive = destructive_looking(action)
    except Exception:
        action, destructive = Action(str(tool_name), "unreadable"), True
    outcome = "warn" if level <= 1 else ("deny" if destructive else "allow")
    reason = f"vscore could not decide ({detail}); {'allowed with a warning' if outcome == 'warn' else outcome + 'ed'}"
    return failure(code, outcome, f"{reason} (failure table: {code}).", action, budget(report, cfg), level)


def report_path(root: str) -> str:
    return os.environ.get("VSCORE_REPORT") or str(Path(root) / ".vscore" / "report.json")


def _broker_hint(decision: Decision, prices: dict | None) -> str:
    urls = (prices or {}).get("prices", {}).get("databases", [])
    statements = list(dict.fromkeys(i.target.detail for i in decision.price.items if i.target.node.startswith("table:")))
    if not urls or not statements:
        return ""
    sql = "; ".join(statements)
    return f" Run it through the broker, which snapshots first: vscore broker sql {shlex.quote(urls[0])} {shlex.quote(sql)}"


def respond(decision: Decision, prices: dict | None, note: str = "") -> dict | None:
    """The hook's JSON answer for a decision, or None to stay silent. `note` is added to an ask."""
    outcome = decision.outcome
    if outcome == "allow":
        return None
    message = decision.message() + (note if outcome == "ask" else "")
    brokerable = decision.via_broker is not None and decision.via_broker.verdict != "block"
    if outcome == "warn":
        prefix = f"[warn mode: enforce would {decision.verdict.replace('_', ' ')}] " if decision.effective == "warn" else ""
        return {"systemMessage": prefix + message + (_broker_hint(decision, prices) if brokerable else "")}
    if outcome == "deny" and (decision.effective == "allow_after_snapshot" or brokerable):
        message += _broker_hint(decision, prices)
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": outcome,
                                   "permissionDecisionReason": message}}


def claude_code(payload: dict, policy: str = "vscore") -> dict | None:
    cwd = payload.get("cwd") or os.getcwd()
    root = repo_root(cwd)
    if root is None:
        return None
    cfg = load_config(root)
    tool_name, tool_input = payload.get("tool_name", ""), payload.get("tool_input", {}) or {}
    prices = load_latest(root)
    if policy == "rules":
        decision = rules_evaluate(tool_name, tool_input, root, cwd, prices, cfg.gate_mode)
    else:  # the gate checks the saved prices against the current commit and recomputes stale ones
        report = load_report(report_path(root))
        try:
            decision = _within(cfg.time_limit_s + GRACE_S,
                               lambda: evaluate(tool_name, tool_input, root, cwd, prices, cfg, report))
        except TimeoutError as exc:
            decision = fault("unavailable", str(exc), tool_name, tool_input, root, cwd, cfg, report)
        except Exception as exc:
            decision = fault("internal_error", f"{type(exc).__name__}: {exc}", tool_name, tool_input, root, cwd, cfg,
                             report)
        if decision.freshness != "fresh":
            prices = load_latest(root)
    note = ""
    if policy == "vscore" and decision.outcome == "ask":
        decision, note = _redeem(decision, root, cfg)
    AuditLog(gate_log_path(Repo(root))).append({
        "at": datetime.now(UTC).isoformat(timespec="seconds"), "policy": policy,
        "session": payload.get("session_id"), **decision.to_dict(),
    })
    return respond(decision, prices, note)


def _redeem(decision: Decision, root: str, cfg: Config) -> tuple[Decision, str]:
    """An ask: run it on a person's approval of this very action and state, once; otherwise file it so a
    person can approve it, and say how."""
    approvals = Approvals(Repo(root))
    grant, problem = approvals.redeem(decision.action_hash, decision.state_hash)
    if grant is not None:
        return replace(decision, approval={"decision_id": grant["decision_id"], "by": grant["by"]}, expires=None), ""
    approvals.request(decision.to_dict())
    minutes = cfg.approval_ttl_s / 60
    note = (f" ({problem}.)" if problem else "") + (
        f" A person can approve this once, within {minutes:g} min and as long as nothing it touches changes:"
        f" vscore approve {shlex.quote(root)} {decision.decision_id}")
    return decision, note


def settings_snippet(policy: str = "compare") -> str:
    command = "vscore hook claude-code" + {"rules": " --rules", "vscore": " --gate"}.get(policy, "")
    snippet = {"hooks": {"PreToolUse": [{"matcher": MATCHER, "hooks": [{"type": "command", "command": command}]}]}}
    return json.dumps(snippet, indent=2) + "\n"
