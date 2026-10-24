"""How much a bug report may spend (plan: Pricing evidence).

budget(r) = stakes(r) * P(true | independent checks), in engineering-hours. Severity raises what is at
stake; only checks the agent does not control raise the chance the report is true. An unverified
report is capped at band 1 (under 10 hours) whatever it claims, so an injected instruction or a
frightening report cannot buy a deletion.

The budget is spent only on the nodes the report points at (localization). Destroying anything else
draws on the default session budget, so a verified bug in a migration file cannot pay for wiping the
customers table: the gate prefers the narrow fix, the way a person would.

Report format (JSON):
{"id": "BUG-12", "title": "...", "impact": "none" | "degraded" | "outage", "users_affected": 120,
 "data_at_risk": false, "stakes_hours": null, "nodes": ["file:app/billing.py"],
 "evidence": [{"kind": "reproduced_by_harness" | "failing_ci_test" | "telemetry" | "independent_report"
               | "agent_claim", "ref": "..."}]}
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from vscore.config import Config

IMPACT_HOURS = {"none": 1.0, "degraded": 10.0, "outage": 100.0}
# Chance the report is true given one check of each kind; the agent's own claim counts for nothing.
CHECK_STRENGTH = {"reproduced_by_harness": 0.9, "failing_ci_test": 0.8, "telemetry": 0.6, "independent_report": 0.3,
                  "agent_claim": 0.0}
PRIOR = 0.1  # an unverified report


@dataclass(frozen=True)
class Budget:
    hours: float
    stakes_hours: float
    p_true: float
    verified: bool
    notes: tuple[str, ...]
    scope: frozenset[str] | None = None  # nodes the evidence is about; None means everything
    outside_hours: float = 0.0  # what may be spent on nodes outside the scope

    def covers(self, node: str) -> bool:
        return self.scope is None or node in self.scope


def stakes(report: dict) -> float:
    if report.get("stakes_hours") is not None:
        return float(report["stakes_hours"])
    base = IMPACT_HOURS.get(report.get("impact", "none"), 1.0)
    users = int(report.get("users_affected", 0) or 0)
    hours = base * (1 + math.log10(1 + users))
    return hours * 3 if report.get("data_at_risk") else hours


def budget(report: dict | None, cfg: Config) -> Budget:
    if report is None:
        return Budget(cfg.default_budget_hours, 0.0, 0.0, False, ("no bug report: default session budget",))
    checks = [e for e in report.get("evidence", []) if CHECK_STRENGTH.get(e.get("kind"), 0) > 0]
    p_false = 1 - PRIOR
    for check in checks:
        p_false *= 1 - CHECK_STRENGTH[check["kind"]]
    p_true = 1 - p_false
    stake = stakes(report)
    hours = stake * p_true
    notes = [f"stakes {stake:.0f} h", f"P(true) {p_true:.2f} from {len(checks)} independent checks"]
    verified = bool(checks)
    if not verified and hours > cfg.unverified_budget_cap_hours:
        hours = cfg.unverified_budget_cap_hours
        notes.append(f"unverified: capped at {cfg.unverified_budget_cap_hours:.0f} h")
    scope = frozenset(report.get("nodes") or ()) or None
    if scope is None:
        hours /= 2
        notes.append("not localized to specific nodes: halved")
    else:
        notes.append(f"scope: {len(scope)} nodes; anything else gets the default {cfg.default_budget_hours:.0f} h")
    return Budget(hours, stake, p_true, verified, tuple(notes), scope, cfg.default_budget_hours)


def load_report(path: str | Path | None) -> dict | None:
    if path is None or not Path(path).exists():
        return None
    return json.loads(Path(path).read_text())
