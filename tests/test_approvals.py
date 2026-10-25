"""Approvals bound to one decision (integration plan, contract v1: Approvals): an approval names a
decision id, carries that decision's action hash and state hash, expires, and allows one execution.
The executor (the Claude Code hook, the broker) recomputes the state hash immediately before running."""

import io
import json
import sqlite3
import time
from contextlib import redirect_stderr, redirect_stdout

import pytest

from vscore import cli
from vscore.bench.scenarios import SCENARIOS
from vscore.bench.study import prepare
from vscore.broker import Broker, BrokerRefused
from vscore.config import Config
from vscore.hooks import claude_code
from vscore.prices.run import load_latest

SCRIPT = "python3 scripts/wipe.py data/app.sqlite"  # a script vscore cannot read, naming the database: ask


@pytest.fixture
def repo(tmp_path, monkeypatch):
    path = tmp_path / "safe_cleanup"
    prepare(SCENARIOS["safe_cleanup"], path, "C", Config())
    monkeypatch.delenv("VSCORE_GATE_MODE", raising=False)
    monkeypatch.setenv("VSCORE_LEVEL", "2")
    monkeypatch.setenv("VSCORE_REPORT", str(path / ".vscore" / "report.json"))
    monkeypatch.delenv("VSCORE_CONFIG", raising=False)
    return path


def ask(repo, command=SCRIPT):
    answer = claude_code({"cwd": str(repo), "session_id": "s", "tool_name": "Bash", "tool_input": {"command": command}})
    record = json.loads((repo / ".git" / "vscore-cache" / "gate.jsonl").read_text().splitlines()[-1])
    outcome = "allow" if answer is None else answer.get("hookSpecificOutput", {}).get("permissionDecision", "warn")
    return outcome, record, answer


def approve(repo, decision_id, by="maintainer") -> int:
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return cli.main(["approve", str(repo), decision_id, "--by", by])


def change_rows(repo, table="customers") -> None:
    db = sqlite3.connect(repo / "data" / "app.sqlite")
    db.execute(f"DELETE FROM {table} WHERE rowid = (SELECT MAX(rowid) FROM {table})")
    db.commit()
    db.close()


def test_every_decision_carries_an_id_and_the_hashes_it_binds_to(repo):
    outcome, record, answer = ask(repo)
    assert outcome == "ask"
    assert len(record["decision_id"]) == 16 and len(record["action_hash"]) == 64 and len(record["state_hash"]) == 64
    assert record["expires"] is not None
    assert record["decision_id"] in answer["hookSpecificOutput"]["permissionDecisionReason"]  # how to approve it
    _, again, _ = ask(repo)
    assert again["decision_id"] != record["decision_id"]
    assert (again["action_hash"], again["state_hash"]) == (record["action_hash"], record["state_hash"])
    allowed = ask(repo, "ls")[1]
    assert allowed["outcome"] == "allow" and allowed["expires"] is None


def test_an_approval_allows_exactly_one_execution(repo):
    _, record, _ = ask(repo)
    assert approve(repo, record["decision_id"]) == 0
    outcome, used, _ = ask(repo)
    assert outcome == "allow"
    assert used["approval"] == {"decision_id": record["decision_id"], "by": "maintainer"}
    assert ask(repo)[0] == "ask"  # used once


def test_an_approval_lapses_when_the_state_changes(repo):
    _, record, _ = ask(repo)
    approve(repo, record["decision_id"])
    change_rows(repo)  # the database the script names changed after the person looked
    outcome, again, answer = ask(repo)
    assert outcome == "ask" and again["approval"] is None
    assert "changed since" in answer["hookSpecificOutput"]["permissionDecisionReason"]


def test_an_approval_lapses_when_it_expires(repo, tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text("[vscore]\napproval_ttl_s = 0.3\n")
    monkeypatch.setenv("VSCORE_CONFIG", str(config))
    _, record, _ = ask(repo)
    approve(repo, record["decision_id"])
    time.sleep(0.4)
    outcome, _, answer = ask(repo)
    assert outcome == "ask" and "expired" in answer["hookSpecificOutput"]["permissionDecisionReason"]


def test_an_approval_covers_only_its_own_action(repo):
    _, record, _ = ask(repo)
    approve(repo, record["decision_id"])
    assert ask(repo, "python3 scripts/wipe.py --all data/app.sqlite")[0] == "ask"
    assert ask(repo)[0] == "allow"  # still unused, so the approved action itself runs


def test_only_open_asks_can_be_approved(repo):
    assert approve(repo, "0" * 16) != 0  # unknown
    _, denied, _ = ask(repo, "rm data/app.sqlite")
    assert denied["outcome"] == "deny" and approve(repo, denied["decision_id"]) != 0  # deny is not approvable
    _, record, _ = ask(repo)
    assert approve(repo, record["decision_id"]) == 0
    assert approve(repo, record["decision_id"]) != 0  # already approved


def test_the_agent_cannot_approve_its_own_action(repo):
    _, record, _ = ask(repo)
    assert ask(repo, f"vscore approve . {record['decision_id']}")[0] == "deny"
    assert ask(repo, "echo '{}' > .git/vscore-cache/approvals/granted/x.json")[0] == "deny"


def test_the_broker_runs_an_irreversible_change_once_per_approval(repo):
    broker = Broker(repo, Config(level=2))
    report = json.loads((repo / ".vscore" / "report.json").read_text())
    report = {**report, "nodes": [*report["nodes"], "table:app.main.customers"], "stakes_hours": 50}
    sql = "DELETE FROM customers WHERE id = 1"  # rows matching a WHERE clause: covered but irreversible
    with pytest.raises(BrokerRefused) as refused:
        broker.run_sql("sqlite:///data/app.sqlite", sql, load_latest(repo), report)
    decision_id = str(refused.value).split("vscore approve ")[1].split()[1]
    assert approve(repo, decision_id) == 0
    assert broker.run_sql("sqlite:///data/app.sqlite", sql, load_latest(repo), report)["result"] == "ok"
    with pytest.raises(BrokerRefused):
        broker.run_sql("sqlite:///data/app.sqlite", sql, load_latest(repo), report)
