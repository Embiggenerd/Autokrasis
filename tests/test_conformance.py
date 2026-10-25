"""Decision conformance (agent integration plan: Conformance suite, layer 1): every case in
vscore/conformance.py, through every door into the core, gives the required outcome and the same
decision record."""

import io
import json
import shutil
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from vscore import cli
from vscore.bench.scenarios import SCENARIOS
from vscore.bench.study import prepare
from vscore.config import Config
from vscore.conformance import CASES, PER_DECISION, RUNS
from vscore.gate import evaluate
from vscore.hooks import claude_code
from vscore.prices.run import load_latest

RUNNING = {"allow", "warn"}


@pytest.fixture(scope="session")
def scenarios(tmp_path_factory):
    """Each scenario built and priced once, as the study prepares its priced arm."""
    root = tmp_path_factory.mktemp("conformance")
    built = {}
    for name, scenario in SCENARIOS.items():
        prepare(scenario, root / name, "C", Config())
        built[name] = root / name
    return built


def _git(repo: Path, *args: str) -> None:
    env = {"GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com", "GIT_COMMITTER_NAME": "Dev",
           "GIT_COMMITTER_EMAIL": "dev@example.com", "GIT_CONFIG_GLOBAL": "/dev/null", "PATH": "/usr/bin:/bin",
           "GIT_AUTHOR_DATE": "2024-02-01T00:00:00Z", "GIT_COMMITTER_DATE": "2024-02-01T00:00:00Z"}
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


def _commit_new_module(repo: Path) -> None:
    """A valuable module, committed after the saved prices were computed."""
    body = "".join(f"def rate_{i}(units):\n    return units * {i * 7919 % 104729} // {i * 31 % 97 + 1}"
                   f" + {i * 271 % 997}\n\n\n" for i in range(60))
    (repo / "app" / "rates.py").write_text(body)
    reports = repo / "app" / "reports.py"
    reports.write_text("from app.rates import rate_1\n" + reports.read_text())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "rates")


def repository(case, scenarios, tmp_path, door: str = "") -> Path:
    """The case's repository. A case that changes it gets a fresh copy per door, so no door sees the
    prices another door recomputed."""
    if not case.setup:
        return scenarios[case.scenario]
    repo = tmp_path / door / case.scenario
    shutil.copytree(scenarios[case.scenario], repo, symlinks=True)
    if case.setup == "commit_new_module":
        _commit_new_module(repo)
    elif case.setup == "delete_prices":
        (repo / ".git" / "vscore-cache" / "prices" / "latest.json").unlink()
    return repo


def tool_input(case, repo: Path):
    if isinstance(case.input, dict) and "file_path" in case.input:
        return {**case.input, "file_path": str(repo / case.input["file_path"])}
    return case.input


def report_path(case, repo: Path) -> Path:
    return repo / ".vscore" / "report.json" if case.report else repo / ".vscore" / "no-report.json"


def config_file(case, tmp_path: Path, level: int | None = None) -> Path:
    """The case's configuration as a TOML file, which every door reads."""
    values = {"level": case.level if level is None else level, **dict(case.config)}
    path = tmp_path / f"config-{values['level']}.toml"
    path.write_text("[vscore]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in values.items()))
    return path


def library(case, repo: Path, config: Path) -> dict:
    report = json.loads(report_path(case, repo).read_text()) if case.report else None
    return evaluate(case.tool, tool_input(case, repo), str(repo), str(repo), load_latest(repo), Config.load(config),
                    report).to_dict()


def command_line(case, repo: Path, config: Path) -> dict:
    argv = ["gate", str(repo), "--tool", case.tool, "--input", json.dumps(tool_input(case, repo)),
            "--config", str(config)]
    if case.report:
        argv += ["--report", str(report_path(case, repo))]
    out = io.StringIO()
    with redirect_stdout(out):
        cli.main(argv)
    return json.loads(out.getvalue())


def hook(case, repo: Path, config: Path, monkeypatch) -> tuple[dict | None, dict]:
    monkeypatch.delenv("VSCORE_GATE_MODE", raising=False)
    monkeypatch.setenv("VSCORE_CONFIG", str(config))
    monkeypatch.setenv("VSCORE_REPORT", str(report_path(case, repo)))
    answer = claude_code({"cwd": str(repo), "session_id": "conformance", "tool_name": case.tool,
                          "tool_input": tool_input(case, repo)})
    log = repo / ".git" / "vscore-cache" / "gate.jsonl"
    return answer, json.loads(log.read_text().splitlines()[-1])


def hook_outcome(answer: dict | None) -> str:
    if answer is None:
        return "allow"
    if "hookSpecificOutput" in answer:
        return answer["hookSpecificOutput"]["permissionDecision"]
    return "warn"


def marks(case):
    return [pytest.mark.xfail(strict=True, reason=case.pending)] if case.pending else []


@pytest.mark.parametrize("case", [pytest.param(c, id=c.name, marks=marks(c)) for c in CASES])
def test_every_door_gives_the_required_outcome_and_the_same_record(case, scenarios, tmp_path, monkeypatch):
    config = config_file(case, tmp_path)
    record = library(case, repository(case, scenarios, tmp_path, "library"), config)
    assert record["level"] == case.level
    assert (record["outcome"] in RUNNING) == (case.expect == RUNS), (record["error"], record["reasons"])
    assert len(record["decision_id"]) == 16 and (record["expires"] is None) == (record["outcome"] != "ask")
    same = {k: v for k, v in record.items() if k not in PER_DECISION}
    answer, logged = hook(case, repository(case, scenarios, tmp_path, "hook"), config, monkeypatch)
    assert hook_outcome(answer) == record["outcome"]
    assert {k: logged[k] for k in same} == same
    printed = command_line(case, repository(case, scenarios, tmp_path, "cli"), config)
    assert {k: printed[k] for k in same} == same


def test_levels_0_and_1_never_stop(scenarios, tmp_path):
    """Every case without a setup step, at levels 0 and 1: nothing stops, and at level 0 nothing interrupts
    except a failure-table warning. The hook turns a decision into its answer as at level 2 (above, and
    tests/test_enforcement.py for warn mode's message), so the library door suffices. The decisions run
    concurrently: each mostly waits on Git."""
    from concurrent.futures import ThreadPoolExecutor

    def check(job):
        case, level = job
        folder = tmp_path / f"{case.name}-{level}"
        folder.mkdir()
        record = library(case, repository(case, scenarios, folder), config_file(case, folder, level))
        allowed = RUNNING if level == 1 or record["error"] else {"allow"}
        return None if record["outcome"] in allowed else f"{case.name} at level {level}: {record['outcome']}"

    jobs = [(case, level) for case in CASES if not case.setup for level in (0, 1)]
    with ThreadPoolExecutor(8) as pool:
        failures = [f for f in pool.map(check, jobs) if f]
    assert not failures


@pytest.mark.parametrize("level,command,expect", [
    (2, "rm data/app.sqlite", "deny"), (2, "ls data", "allow"), (1, "rm data/app.sqlite", "warn"),
])
@pytest.mark.parametrize("fault", ["internal_error", "unavailable"])
def test_hook_faults_follow_the_failure_table(fault, level, command, expect, scenarios, tmp_path, monkeypatch):
    """A crash inside the core, or a core slower than the time limit: destructive-looking actions are
    denied at levels 2 and 3 and everything else runs; at levels 0 and 1 everything runs, with a warning."""
    import time

    from vscore import hooks

    def broken(*args, **kwargs):
        if fault == "unavailable":
            time.sleep(0.6)  # beyond the 0.2 s limit plus the hook's grace
        raise RuntimeError("simulated fault")

    monkeypatch.setattr(hooks, "evaluate", broken)
    monkeypatch.setattr(hooks, "GRACE_S", 0.1)
    config = tmp_path / "config.toml"
    config.write_text(f"[vscore]\nlevel = {level}\ntime_limit_s = 0.2\n")
    monkeypatch.setenv("VSCORE_CONFIG", str(config))
    repo = scenarios["corrupted_test_state"]
    answer = claude_code({"cwd": str(repo), "tool_name": "Bash", "tool_input": {"command": command}})
    assert hook_outcome(answer) == expect
    logged = json.loads((repo / ".git" / "vscore-cache" / "gate.jsonl").read_text().splitlines()[-1])
    assert logged["error"] == fault and logged["outcome"] == expect
