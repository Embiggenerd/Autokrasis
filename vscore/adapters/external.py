"""Events from sources outside Git: CI runs, code review, incidents and production traffic.

Each reads a local file in a documented format, so ingestion never needs network access:

- CI runs, JSON lines: {"run_id", "sha", "at", "protected": bool, "conclusion": "success"|"failure",
  "tests": int?, "failures": int?, "workflow": str?}
- Reviews, the JSON array from `gh pr list --state merged --json number,author,mergedAt,mergeCommit,commits,reviews,files`
- Incidents, JSON lines: {"id", "at", "severity": 1-4 (1 worst), "paths": [...]?, "commits": [...]?}
- Traffic, JSON lines: {"path", "at", "requests": int, "errors": int?}
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from vscore.events import ActorHasher, Event
from vscore.nodes import file_node


def _json_lines(path: str | Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def ci_events(path: str | Path) -> list[Event]:
    events = []
    for run in _json_lines(path):
        passed = run["conclusion"] == "success"
        protected = bool(run.get("protected"))
        events.append(Event(
            source="ci",
            kind="test_run",
            at=run["at"],
            key=str(run["run_id"]),
            payload={"conclusion": run["conclusion"], "protected": protected, "tests": run.get("tests"),
                     "failures": run.get("failures"), "workflow": run.get("workflow")},
            rev=run["sha"],
            episode=run["sha"],
            verified_by=f"ci:{run['run_id']}" if passed and protected else None,
        ))
    return events


def review_events(path: str | Path, actors: ActorHasher) -> list[Event]:
    events = []
    for pr in json.loads(Path(path).read_text()):
        author = (pr.get("author") or {}).get("login", "")
        approvers = sorted({
            r["author"]["login"] for r in pr.get("reviews", [])
            if r.get("state") == "APPROVED" and r.get("author") and r["author"]["login"] != author
        })
        commits = [c["oid"] for c in pr.get("commits", []) if c.get("oid")]
        if (pr.get("mergeCommit") or {}).get("oid"):
            commits.append(pr["mergeCommit"]["oid"])
        events.append(Event(
            source="review",
            kind="review",
            at=pr.get("mergedAt") or "",
            key=f"pr:{pr['number']}",
            nodes=tuple(file_node(f["path"]) for f in pr.get("files", [])),
            payload={"approvals": len(approvers), "reviews": len(pr.get("reviews", []))},
            episode=f"pr:{pr['number']}",
            actor_kind="agent" if author.endswith("[bot]") else "human",
            actor_id=actors(author) if author else None,
            verified_by=f"review:{pr['number']}" if approvers else None,
            links=tuple(sorted(set(commits))),
        ))
    return events


def incident_events(path: str | Path) -> list[Event]:
    return [
        Event(
            source="incident",
            kind="incident",
            at=item["at"],
            key=str(item["id"]),
            nodes=tuple(file_node(p) for p in item.get("paths", [])),
            payload={"severity": int(item["severity"])},
            links=tuple(item.get("commits", [])),
        )
        for item in _json_lines(path)
    ]


def traffic_events(path: str | Path) -> list[Event]:
    return [
        Event(
            source="telemetry",
            kind="traffic",
            at=item["at"],
            key=f"{item['path']}@{item['at']}",
            nodes=(file_node(item["path"]),),
            payload={"requests": int(item["requests"]), "errors": int(item.get("errors", 0))},
        )
        for item in _json_lines(path)
    ]


def fetch_github_prs(repo_slug: str, limit: int, output: str | Path) -> int:
    """Runs the GitHub CLI with the user's own credentials and writes merged PRs in the review format."""
    fields = "number,author,mergedAt,mergeCommit,commits,reviews,files"
    proc = subprocess.run(
        ["gh", "pr", "list", "--repo", repo_slug, "--state", "merged", "--limit", str(limit), "--json", fields],
        capture_output=True, text=True, check=True,
    )
    Path(output).write_text(proc.stdout)
    return len(json.loads(proc.stdout))
