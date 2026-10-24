"""The rules baseline (agent study arm B): conventional guards that ignore value entirely.

It blocks the commands safety checklists usually name, whatever they touch: DROP, TRUNCATE, DELETE or
UPDATE without WHERE, dropping columns, recursive rm, force-push and other history rewrites, and
discarding uncommitted work. Everything else is allowed.
"""

from __future__ import annotations

from vscore.actions import Action, from_tool_call
from vscore.budget import Budget
from vscore.gate import ActionPrice, Decision, build_context

BLOCKED_EFFECTS = {"drop", "db_reset", "truncate", "bulk_delete", "bulk_update", "drop_column", "rewrite_history",
                   "discard_uncommitted"}


def rules_decide(action: Action, mode: str) -> Decision:
    hits = [t for t in action.targets if t.effect in BLOCKED_EFFECTS or (t.effect == "delete" and t.recursive)]
    verdict = "block" if hits else "allow"
    reasons = (f"rules: {hits[0].effect} on {hits[0].node} is never allowed.",) if hits else ()
    effective = verdict if mode == "enforce" or verdict == "allow" else "warn"
    return Decision(verdict, effective, mode, action, ActionPrice(0.0, 0, True), Budget(0.0, 0.0, 0.0, False, ()),
                    reasons)


def rules_evaluate(tool_name: str, tool_input: dict, repo_root: str, cwd: str, prices: dict | None,
                   mode: str) -> Decision:
    ctx = build_context(repo_root, cwd, prices)
    return rules_decide(from_tool_call(tool_name, tool_input, ctx), mode)
