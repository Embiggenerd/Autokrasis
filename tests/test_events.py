import json
import sqlite3

import pytest

from vscore.adapters.external import ci_events, incident_events, review_events, traffic_events
from vscore.adapters.gitlog import git_events, is_agent, parse_log
from vscore.events import ActorHasher, Event, EventStore
from vscore.evidence import UNKNOWN, UNVERIFIED, VERIFIED, gather
from vscore.gitrepo import Repo
from vscore.nodes import Lineage


def test_store_is_append_only_and_idempotent(tmp_path):
    store = EventStore(tmp_path / "events.sqlite")
    event = Event(source="ci", kind="test_run", at="2024-01-01T00:00:00Z", key="run-1", payload={"conclusion": "success"})
    assert store.add([event]) == 1
    assert store.add([event]) == 0  # re-ingesting the same fact changes nothing
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.db.execute("DELETE FROM events")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.db.execute("UPDATE events SET kind = 'deploy'")
    assert store.window("ci") == ("2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z")
    assert store.window("review") is None


def test_events_reject_unknown_sources_and_kinds():
    with pytest.raises(ValueError):
        Event(source="slack", kind="report", at="", key="x")
    with pytest.raises(ValueError):
        Event(source="git", kind="praise", at="", key="x")


def test_actor_hashes_are_keyed_stable_and_hide_identity(tmp_path):
    hasher = ActorHasher(tmp_path / "actor.key")
    assert hasher("Dev@Example.com") == ActorHasher(tmp_path / "actor.key")("dev@example.com")
    assert "dev" not in hasher("dev@example.com")
    assert hasher("dev@example.com") != ActorHasher(tmp_path / "other.key")("dev@example.com")


def test_agent_detection_uses_author_and_trailers():
    assert is_agent("bot@x.com", "Claude Opus <noreply@anthropic.com>")
    assert is_agent("dependabot[bot]@users.noreply.github.com", "")
    assert not is_agent("dev@example.com", "")


def test_parse_log_reads_renames_and_plain_changes():
    output = (
        b"\x1eaaa\x1fppp\x1f2024-01-01T00:00:00+00:00\x1fdev@x.com\x1f\x00\n"
        b":100644 100644 1111 2222 R090\x00old.py\x00new.py\x00"
        b":000000 100644 0000 3333 A\x00fresh.py\x00"
    )
    [commit] = parse_log(output)
    assert commit["sha"] == "aaa" and commit["parents"] == ["ppp"]
    assert commit["files"] == [
        {"status": "R", "old_path": "old.py", "path": "new.py"},
        {"status": "A", "old_path": None, "path": "fresh.py"},
    ]


def test_git_events_and_lineage_follow_renames(make_repo, tmp_path):
    repo = make_repo()
    repo.write("old.txt", "".join(f"line {i}\n" for i in range(30)))
    first = repo.commit("add")
    repo.move("old.txt", "new.txt")
    repo.commit("rename")
    git = Repo(repo.path)
    events = git_events(git, "HEAD", ActorHasher(tmp_path / "k"))
    assert {(e.rev, e.kind) for e in events} >= {(first, "create")}
    lineage = Lineage.from_git(git, "HEAD", {"new.txt"})
    assert lineage.current("old.txt") == "new.txt"
    assert lineage.current("gone.txt") is None


def write_lines(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_verification_is_verified_unverified_or_unknown(make_repo, tmp_path):
    repo = make_repo()
    repo.write("a.py", "x = 1\n")
    old = repo.commit("before CI existed")
    repo.write("a.py", "x = 2\n")
    checked = repo.commit("checked by CI")
    repo.write("a.py", "x = 3\n")
    reviewed = repo.commit("reviewed")
    repo.write("a.py", "x = 4\n")
    unchecked = repo.commit("never checked")
    git = Repo(repo.path)
    actors = ActorHasher(tmp_path / "k")
    store = EventStore(tmp_path / "events.sqlite")
    store.add(git_events(git, "HEAD", actors))
    dates = {e.rev: e.at for e in git_events(git, "HEAD", actors)}

    write_lines(tmp_path / "ci.jsonl", [
        {"run_id": 1, "sha": checked, "at": dates[checked], "protected": True, "conclusion": "success"},
        {"run_id": 2, "sha": unchecked, "at": dates[unchecked], "protected": False, "conclusion": "success"},
    ])
    (tmp_path / "prs.json").write_text(json.dumps([{
        "number": 7, "author": {"login": "ann"}, "mergedAt": dates[reviewed],
        "mergeCommit": {"oid": reviewed}, "commits": [{"oid": reviewed}],
        "reviews": [{"state": "APPROVED", "author": {"login": "bob"}}], "files": [{"path": "a.py"}],
    }]))
    store.add(ci_events(tmp_path / "ci.jsonl"))
    store.add(review_events(tmp_path / "prs.json", actors))
    evidence = gather(store, Lineage.from_git(git, "HEAD", {"a.py"}))

    assert evidence.verification(checked) == VERIFIED
    assert evidence.verification(reviewed) == VERIFIED
    assert evidence.verification(unchecked) == UNVERIFIED  # CI ran, but not on a protected config
    assert evidence.verification(old) == UNKNOWN  # before any verification source existed


def test_self_approval_does_not_verify(tmp_path):
    (tmp_path / "prs.json").write_text(json.dumps([{
        "number": 1, "author": {"login": "ann"}, "mergedAt": "2024-01-01T00:00:00Z", "commits": [{"oid": "c1"}],
        "reviews": [{"state": "APPROVED", "author": {"login": "ann"}}], "files": [],
    }]))
    [event] = review_events(tmp_path / "prs.json", ActorHasher(tmp_path / "k"))
    assert event.verified_by is None


def test_incidents_and_traffic_attach_to_current_paths(make_repo, tmp_path):
    repo = make_repo()
    repo.write("old.py", "x = 1\n" * 20)
    culprit = repo.commit("add")
    repo.move("old.py", "new.py")
    repo.commit("rename")
    git = Repo(repo.path)
    store = EventStore(tmp_path / "events.sqlite")
    store.add(git_events(git, "HEAD", ActorHasher(tmp_path / "k")))
    write_lines(tmp_path / "inc.jsonl", [{"id": "INC-1", "at": "2024-02-01T00:00:00Z", "severity": 2, "commits": [culprit]}])
    write_lines(tmp_path / "traffic.jsonl", [{"path": "old.py", "at": "2024-02-01T00:00:00Z", "requests": 5000}])
    store.add(incident_events(tmp_path / "inc.jsonl"))
    store.add(traffic_events(tmp_path / "traffic.jsonl"))
    evidence = gather(store, Lineage.from_git(git, "HEAD", {"new.py"}))
    assert evidence.incidents == {"new.py": [2]}
    assert evidence.traffic == {"new.py": 5000}
