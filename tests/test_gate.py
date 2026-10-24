import sqlite3

import pytest

from vscore.audit import AuditLog
from vscore.budget import budget
from vscore.config import Config
from vscore.gate import evaluate
from vscore.prices.run import price_repo

CFG = Config()
ENFORCE = Config(gate_mode="enforce")


def test_budgets():
    assert budget(None, CFG).hours == CFG.default_budget_hours
    scary = {"impact": "outage", "users_affected": 100000, "data_at_risk": True, "nodes": ["file:a.py"],
             "evidence": [{"kind": "agent_claim"}]}
    unverified = budget(scary, CFG)
    assert unverified.hours == CFG.unverified_budget_cap_hours and not unverified.verified
    reproduced = budget({**scary, "evidence": [{"kind": "reproduced_by_harness"}, {"kind": "telemetry"}]}, CFG)
    assert reproduced.verified and reproduced.hours > 1000
    vague = budget({"impact": "degraded", "evidence": [{"kind": "failing_ci_test"}]}, CFG)
    assert "halved" in vague.notes[-1]


@pytest.fixture
def shop(make_repo, tmp_path):
    repo = make_repo()
    repo.write("app/core.py", "".join(f"def f{i}():\n    return {i}\n" for i in range(30)))
    for name in ("a", "b", "c", "d", "e", "f"):
        repo.write(f"app/{name}.py", "from app import core\n")
    repo.write("app/leaf.py", "X = 1\n")
    repo.commit("app")
    db = repo.path / "shop.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE customers (id INTEGER, email TEXT); INSERT INTO customers VALUES (1, 'a@x');"
                       "CREATE TABLE cache_pages (k TEXT);")
    conn.commit()
    conn.close()
    prices = price_repo(repo.path, databases=(f"sqlite:///{db}",)).data
    return repo, prices


def run(shop, command, cfg=ENFORCE, report=None):
    repo, prices = shop
    return evaluate("Bash", {"command": command}, str(repo.path), str(repo.path), prices, cfg, report)


def test_cheap_actions_are_allowed(shop):
    assert run(shop, "ls -la && pytest").verdict == "allow"
    assert run(shop, "sqlite3 shop.sqlite 'DELETE FROM cache_pages'").verdict == "allow"  # disposable table


def test_dropping_production_data_is_blocked_without_evidence(shop):
    decision = run(shop, "sqlite3 shop.sqlite 'DROP TABLE customers'")
    assert decision.verdict == "block" and decision.effective == "block"
    assert decision.price.band == 4
    assert "no tested restore" in decision.message()


def test_warn_mode_reports_instead_of_blocking(shop):
    decision = run(shop, "sqlite3 shop.sqlite 'DROP TABLE customers'", cfg=CFG)
    assert (decision.verdict, decision.effective) == ("block", "warn")


def test_reversible_deletion_within_budget_is_allowed_after_snapshot(shop):
    report = {"impact": "degraded", "users_affected": 50, "nodes": ["file:app/core.py"],
              "evidence": [{"kind": "reproduced_by_harness"}]}
    decision = run(shop, "rm app/core.py", report=report)
    assert decision.verdict == "allow_after_snapshot"
    assert decision.price.reversible
    assert run(shop, "rm app/core.py").verdict == "block"  # 6 importers: too much harm for the default budget


def test_irreversible_but_covered_needs_approval(shop):
    repo, _ = shop
    work = "".join(f"rate_{i} = {i * 7919 % 104729} * {i * 31337 % 65537} / {i * 271 % 997 + 1}\n" for i in range(400))
    (repo.path / "app" / "leaf.py").write_text("X = 1\n" + work)  # hours of uncommitted work
    report = {"stakes_hours": 200, "nodes": ["file:app/leaf.py"], "evidence": [{"kind": "failing_ci_test"}]}
    decision = run(shop, "git checkout -- app/leaf.py", report=report)
    assert decision.verdict == "approve"
    assert not decision.price.reversible


def test_deleting_a_module_and_its_importers_counts_shared_harm_once(shop):
    separate = run(shop, "rm app/core.py").price.hours
    together = run(shop, "rm app/core.py app/a.py app/b.py").price.hours
    assert together < separate + 1  # two importers gone with it: their harm is not double counted


def test_audit_log_is_hash_chained(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append({"verdict": "allow"})
    log.append({"verdict": "block"})
    assert log.verify()
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    (tmp_path / "audit.jsonl").write_text(lines[0].replace("allow", "block") + "\n" + lines[1] + "\n")
    assert not log.verify()
