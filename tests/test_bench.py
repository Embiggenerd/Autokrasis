import json
import shutil

import pytest

from vscore.bench.report import bootstrap_ci, decision_rule, markdown, summarize
from vscore.bench.scenarios import SCENARIOS
from vscore.bench.study import load_results, replay_scenarios, run_scripted, run_study
from vscore.config import Config

needs_sqlite_cli = pytest.mark.skipif(shutil.which("sqlite3") is None, reason="scenarios use the sqlite3 CLI")


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenarios_start_broken_and_unharmed(name, tmp_path):
    scenario = SCENARIOS[name]
    scenario.build(tmp_path / name)
    assert not scenario.accept(tmp_path / name)
    assert scenario.harm(tmp_path / name) == (0.0, [])


@needs_sqlite_cli
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_careful_plan_resolves_without_harm(name, tmp_path):
    result = run_scripted(SCENARIOS[name], "A", "careful", tmp_path / name, Config())
    assert result.resolved and result.harm_hours == 0.0


@needs_sqlite_cli
def test_reckless_plan_causes_the_tempting_harm_without_a_gate(tmp_path):
    result = run_scripted(SCENARIOS["corrupted_test_state"], "A", "reckless", tmp_path / "s", Config())
    assert result.resolved and result.harm_hours == 1000.0


@needs_sqlite_cli
def test_priced_arm_stops_out_of_scope_wipes_and_rules_block_needed_cleanup(tmp_path):
    priced = run_scripted(SCENARIOS["failed_migration"], "C", "reckless", tmp_path / "c", Config())
    assert priced.stopped == 1 and priced.harm_hours == 0.0 and priced.resolved
    rules = run_scripted(SCENARIOS["safe_cleanup"], "B", "careful", tmp_path / "b", Config())
    assert rules.false_interventions == 1 and not rules.resolved


@needs_sqlite_cli
def test_study_writes_results_and_report(tmp_path):
    output = tmp_path / "study.jsonl"
    results = run_study(["corrupted_test_state"], ["A", "C"], "reckless", 1, Config(), output)
    assert len(results) == 2 and len(load_results(output)) == 2
    text = markdown(load_results(output))
    assert "| A Control |" in text and "Decision rule" in text


def test_report_math():
    low, high = bootstrap_ci([0.0, 10.0, 20.0])
    assert 0.0 <= low <= high <= 20.0
    results = [
        {"scenario": "x", "arm": "B", "resolved": True, "harm_hours": 100.0, "false_interventions": 2, "stopped": 2,
         "approvals": 0, "seconds": 1.0},
        {"scenario": "x", "arm": "C", "resolved": True, "harm_hours": 10.0, "false_interventions": 1, "stopped": 1,
         "approvals": 0, "seconds": 1.0},
    ]
    passed, text = decision_rule(summarize(results))
    assert passed and "reduced 90%" in text


@needs_sqlite_cli
def test_replay_over_scenarios_counts_each_policy():
    totals = replay_scenarios(Config())
    assert set(totals) == {"none", "rules", "vscore"}
    assert totals["none"]["missed"] == totals["none"]["harmful"]
    assert json.dumps(totals)
