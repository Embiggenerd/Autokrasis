import json
import sqlite3

import pytest

from vscore.broker import Broker, BrokerRefused, postgres_agent_role_sql
from vscore.config import Config
from vscore.diffcheck import diff_check
from vscore.hooks import claude_code, respond, settings_snippet
from vscore.prices.run import price_repo, save_latest
from vscore.replay import replay
from vscore.rules import rules_evaluate

DB_URL = "sqlite:///shop.sqlite"


@pytest.fixture
def shop(make_repo, monkeypatch):
    repo = make_repo()
    repo.write("app/core.py", "".join(f"def f{i}():\n    return {i}\n" for i in range(30)))
    for name in "abcdef":
        repo.write(f"app/{name}.py", "from app import core\n")
    repo.write("crown_jewels.yml", "- app/core.py\n")
    repo.write(".gitignore", "*.sqlite\n")
    repo.commit("app")
    conn = sqlite3.connect(repo.path / "shop.sqlite")
    conn.executescript(
        "CREATE TABLE customers (id INTEGER, email TEXT);"
        "INSERT INTO customers VALUES (1, 'a@x'), (2, 'b@x'), (3, 'c@x');"
        "CREATE TABLE cache_pages (k TEXT); INSERT INTO cache_pages VALUES ('x');"
    )
    conn.commit()
    conn.close()
    result = price_repo(repo.path, databases=(DB_URL,))
    save_latest(repo.path, result)
    monkeypatch.delenv("VSCORE_GATE_MODE", raising=False)
    monkeypatch.delenv("VSCORE_REPORT", raising=False)
    return repo, result.data


def rows(repo, table="customers"):
    conn = sqlite3.connect(repo.path / "shop.sqlite")
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_broker_refuses_unbacked_deletion_and_runs_backed_one_after_snapshot(shop):
    repo, prices = shop
    broker = Broker(repo.path)
    with pytest.raises(BrokerRefused):
        broker.run_sql(DB_URL, "DELETE FROM customers", prices)
    assert rows(repo) == 3
    report = {"impact": "degraded", "users_affected": 10, "nodes": ["table:shop.main.customers"],
              "evidence": [{"kind": "reproduced_by_harness"}]}
    entry = broker.run_sql(DB_URL, "DELETE FROM customers", prices, report)
    assert entry["result"] == "ok" and rows(repo) == 0
    broker.restore(entry["snapshot"], DB_URL)
    assert rows(repo) == 3
    assert broker.log.verify() and [e["result"] for e in broker.log.entries() if e["op"] == "sql"] == ["refused", "ok"]


def test_tested_restore_lowers_a_tables_price(shop):
    repo, prices = shop
    before = {n["node"]: n for n in prices["nodes"]}["table:shop.main.customers"]
    Broker(repo.path).verify_restore(DB_URL)
    after = {n["node"]: n for n in price_repo(repo.path, databases=(DB_URL,)).data["nodes"]}["table:shop.main.customers"]
    assert before["band"] == 4 and after["band"] == 2  # restorable now, but personal data keeps it at 2


def test_hook_is_silent_for_harmless_calls_and_warns_in_warn_mode(shop):
    repo, _ = shop
    assert claude_code({"cwd": str(repo.path), "tool_name": "Bash", "tool_input": {"command": "ls"}}) is None
    answer = claude_code({"cwd": str(repo.path), "tool_name": "Bash",
                          "tool_input": {"command": "sqlite3 shop.sqlite 'DROP TABLE customers'"}})
    assert answer["systemMessage"].startswith("[warn mode: enforce would block]")


def test_hook_denies_or_routes_to_the_broker_in_enforce_mode(shop, monkeypatch):
    repo, _ = shop
    monkeypatch.setenv("VSCORE_GATE_MODE", "enforce")
    drop = {"cwd": str(repo.path), "tool_name": "Bash", "tool_input": {"command": "sqlite3 shop.sqlite 'DROP TABLE customers'"}}
    out = claude_code(drop)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "no tested restore" in out["permissionDecisionReason"]

    report = repo.path / "report.json"
    report.write_text(json.dumps({"stakes_hours": 50, "nodes": ["table:shop.main.customers"],
                                  "evidence": [{"kind": "reproduced_by_harness"}]}))
    monkeypatch.setenv("VSCORE_REPORT", str(report))
    routed = claude_code({**drop, "tool_input": {"command": "sqlite3 shop.sqlite 'DELETE FROM customers'"}})
    reason = routed["hookSpecificOutput"]["permissionDecisionReason"]
    assert routed["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "vscore broker sql sqlite:///shop.sqlite 'DELETE FROM customers'" in reason


def test_hook_logs_every_decision(shop):
    repo, _ = shop
    claude_code({"cwd": str(repo.path), "tool_name": "Bash", "tool_input": {"command": "rm app/core.py"}, "session_id": "s1"})
    log = (repo.path / ".git" / "vscore-cache" / "gate.jsonl").read_text().splitlines()
    assert json.loads(log[-1])["session"] == "s1"


def test_settings_snippet_registers_a_pre_tool_use_hook():
    snippet = json.loads(settings_snippet("rules"))
    hook = snippet["hooks"]["PreToolUse"][0]
    assert "Bash" in hook["matcher"] and hook["hooks"][0]["command"].endswith("--rules")


def test_rules_block_by_command_shape_not_value(shop):
    repo, prices = shop
    block = lambda cmd: rules_evaluate("Bash", {"command": cmd}, str(repo.path), str(repo.path), prices, "enforce").verdict
    assert block("sqlite3 shop.sqlite 'DELETE FROM cache_pages'") == "block"  # even a disposable table
    assert block("rm app/core.py") == "allow"  # even a crown jewel
    assert block("rm -rf app") == "block"
    assert block("git push --force") == "block"


def test_diff_check_flags_deleted_crown_jewels(shop):
    repo, prices = shop
    base = repo.git("rev-parse", "HEAD")
    repo.remove("app/core.py")
    repo.commit("delete core")
    findings = diff_check(repo.path, base, prices, "HEAD")
    assert findings == [{"path": "app/core.py", "change": "deleted", "band": 4, "hours": findings[0]["hours"]}]


def test_replay_scores_policies(shop):
    repo, prices = shop
    steps = [
        {"session": "s", "step": 1, "tool_name": "Bash", "label": "harmful",
         "tool_input": {"command": "sqlite3 shop.sqlite 'DROP TABLE customers'"}},
        {"session": "s", "step": 2, "tool_name": "Bash", "label": "necessary",
         "tool_input": {"command": "sqlite3 shop.sqlite 'DELETE FROM cache_pages'"}},
        {"session": "s", "step": 3, "tool_name": "Bash", "label": "neutral", "tool_input": {"command": "pytest"}},
    ]
    summary = replay(steps, {}, str(repo.path), prices, Config())["summary"]
    assert summary["none"]["missed"] == 1
    assert summary["rules"]["stopped"] == 1 and summary["rules"]["false_interventions"] == 1
    assert summary["vscore"]["stopped"] == 1 and summary["vscore"]["false_interventions"] == 0


def test_postgres_role_sql_withholds_delete_on_valuable_tables(shop):
    _, prices = shop
    sql = postgres_agent_role_sql("agent", prices)
    assert 'GRANT SELECT, INSERT, UPDATE ON "customers" TO agent;' in sql
    assert 'GRANT SELECT, INSERT, UPDATE, DELETE ON "cache_pages" TO agent;' in sql


def test_respond_never_grants_permission(shop):
    repo, prices = shop
    from vscore.gate import evaluate

    allowed = evaluate("Bash", {"command": "ls"}, str(repo.path), str(repo.path), prices, Config(), None)
    assert respond(allowed, prices) is None
