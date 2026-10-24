"""What the event store says about each commit and file: verification, incidents, traffic."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from vscore.events import EventStore
from vscore.nodes import Lineage, node_path

VERIFIED, UNVERIFIED, UNKNOWN = "verified", "unverified", "unknown"


@dataclass
class Evidence:
    verified_by: dict[str, list[str]] = field(default_factory=dict)  # commit -> independent checks
    windows: dict[str, tuple[str, str]] = field(default_factory=dict)  # verification source -> time span seen
    commit_dates: dict[str, str] = field(default_factory=dict)
    agent_commits: set[str] = field(default_factory=set)
    incidents: dict[str, list[int]] = field(default_factory=dict)  # current path -> severities
    traffic: dict[str, int] = field(default_factory=dict)  # current path -> requests
    restore_tests: dict[str, str] = field(default_factory=dict)  # table node -> time of last tested restore

    def verification(self, commit: str) -> str:
        """`verified` if an independent check covers the commit; `unverified` if a verification source
        was ingested for the commit's time but none covered it; otherwise `unknown` (missing, not zero)."""
        if commit in self.verified_by:
            return VERIFIED
        at = self.commit_dates.get(commit)
        if at and any(start <= at <= end for start, end in self.windows.values()):
            return UNVERIFIED
        return UNKNOWN


def gather(store: EventStore, lineage: Lineage) -> Evidence:
    evidence = Evidence()
    files_by_commit: dict[str, set[str]] = defaultdict(set)
    for event in store.query(source="git"):
        evidence.commit_dates[event["rev"]] = event["at"]
        if event["actor_kind"] == "agent":
            evidence.agent_commits.add(event["rev"])
        files_by_commit[event["rev"]].update(node_path(n) for n in event["nodes"] if node_path(n))

    for source in ("ci", "review"):
        if window := store.window(source):
            evidence.windows[source] = window
    for event in store.query(source="ci"):
        if event["verified_by"]:
            evidence.verified_by.setdefault(event["rev"], []).append(event["verified_by"])
    for event in store.query(source="review"):
        if event["verified_by"]:
            for commit in event["links"]:
                evidence.verified_by.setdefault(commit, []).append(event["verified_by"])

    incidents: dict[str, list[int]] = defaultdict(list)
    for event in store.query(source="incident"):
        paths = {node_path(n) for n in event["nodes"]} | {p for c in event["links"] for p in files_by_commit.get(c, ())}
        for path in {lineage.current(p) for p in paths if p} - {None}:
            incidents[path].append(event["payload"]["severity"])
    evidence.incidents = {p: sorted(s) for p, s in incidents.items()}

    traffic: dict[str, int] = defaultdict(int)
    for event in store.query(source="telemetry"):
        for node in event["nodes"]:
            if (path := node_path(node)) and (current := lineage.current(path)):
                traffic[current] += event["payload"]["requests"]
    evidence.traffic = dict(traffic)

    for event in store.query(source="schema", kind="test_run"):
        if event["verified_by"]:
            for node in event["nodes"]:
                evidence.restore_tests[node] = max(evidence.restore_tests.get(node, ""), event["at"])
    return evidence
