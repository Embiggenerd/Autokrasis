"""Git history as events: one event per file a non-merge commit touched."""

from __future__ import annotations

from vscore.events import ActorHasher, Event
from vscore.gitrepo import Repo, decode_path

_STATUS_KIND = {"A": "create", "M": "modify", "T": "modify", "D": "delete", "R": "rename", "C": "create"}

# Commit trailers and author names that mark a change as written by a coding agent.
AGENT_MARKERS = ("claude", "copilot", "codex", "devin", "cursor", "aider", "[bot]", "noreply@anthropic.com")


def is_agent(author_email: str, trailers: str) -> bool:
    text = f"{author_email} {trailers}".lower()
    return any(marker in text for marker in AGENT_MARKERS)


def parse_log(output: bytes) -> list[dict]:
    """Commits from `git log --raw -z --format=%x1e%H%x1f%P%x1f%aI%x1f%ae%x1f%(trailers...)`."""
    commits = []
    for record in output.split(b"\x1e")[1:]:
        header, _, rest = record.partition(b"\0")
        sha, parents, at, email, trailers = header.decode("utf-8", "replace").split("\x1f")
        tokens = rest.lstrip(b"\n").split(b"\0")
        files = []
        i = 0
        while i < len(tokens):
            token = tokens[i].lstrip(b"\n")
            if not token.startswith(b":"):
                i += 1
                continue
            status = token.split(b" ")[-1].decode()[:1]
            if status in ("R", "C"):
                files.append({"status": status, "old_path": decode_path(tokens[i + 1]), "path": decode_path(tokens[i + 2])})
                i += 3
            else:
                files.append({"status": status, "old_path": None, "path": decode_path(tokens[i + 1])})
                i += 2
        commits.append({"sha": sha, "parents": parents.split(), "at": at, "email": email, "trailers": trailers.strip(),
                        "files": files})
    return commits


def git_events(repo: Repo, rev: str, actors: ActorHasher) -> list[Event]:
    output = repo.run(
        "log", "--no-merges", "-M", "--raw", "-z", "--no-abbrev",
        "--format=%x1e%H%x1f%P%x1f%aI%x1f%ae%x1f%(trailers:key=Co-authored-by,valueonly,separator=%x2c)", rev,
    )
    events = []
    for commit in parse_log(output):
        kind = "agent" if is_agent(commit["email"], commit["trailers"]) else "human"
        for f in commit["files"]:
            events.append(Event(
                source="git",
                kind=_STATUS_KIND.get(f["status"], "modify"),
                at=commit["at"],
                key=f"{commit['sha']}:{f['path']}",
                nodes=(f"file:{f['path']}",),
                payload={"status": f["status"], "old_path": f["old_path"]},
                rev=commit["sha"],
                episode=commit["sha"],
                actor_kind=kind,
                actor_id=actors(commit["email"]),
            ))
    return events
