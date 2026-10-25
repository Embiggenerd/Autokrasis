"""The four-arm agent study (plan: Agent study).

| Arm | What the agent has                    | Question answered                                 |
| A   | standard model and tools              | how often does unprotected behavior cause loss?   |
| B   | plus conventional safety rules        | do simple guards solve it?                        |
| C   | plus prices, budgets and the gate     | does the currency add measurable value?           |
| D   | plus full raw history, no prices      | do prices keep what decisions need from history?  |

Two kinds of agent run here. Scripted agents follow a scenario's reckless or careful plan through
the arm's policy; they test the instrumentation end to end (the plan's first step). A command agent
runs any coding-agent CLI on the scenario; arms B and C install the PreToolUse hook in the scenario's
`.claude/settings.json`. Every arm gets the same task, repository, database and budget.
"""

from __future__ import annotations

import json
import os
import random
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from vscore.audit import AuditLog
from vscore.bench.scenarios import DB_URL, SCENARIOS, Scenario, Step
from vscore.broker import Broker
from vscore.config import Config
from vscore.gate import Decision, evaluate, gate_log_path
from vscore.gitrepo import Repo
from vscore.prices.run import price_repo, save_latest
from vscore.replay import replay
from vscore.rules import rules_evaluate

ARMS = {"A": "control", "B": "rules", "C": "vscore", "D": "history"}


@dataclass
class RunResult:
    scenario: str
    arm: str
    agent: str
    replicate: int
    resolved: bool = False
    harm_hours: float = 0.0
    harm_notes: list[str] = field(default_factory=list)
    steps_run: int = 0
    stopped: int = 0
    approvals: int = 0
    false_interventions: int = 0
    snapshots: int = 0
    harm_after_restore: float | None = None  # harm left once the run's first snapshot is restored
    seconds: float = 0.0
    error: str | None = None


def prepare(scenario: Scenario, workdir: Path, arm: str, cfg: Config) -> dict:
    """Builds the scenario, writes its bug report, and prices it, identically for every arm."""
    scenario.build(workdir)
    (workdir / ".vscore").mkdir(exist_ok=True)
    (workdir / ".vscore" / "report.json").write_text(json.dumps(scenario.report, indent=2))
    result = price_repo(workdir, cfg=cfg, databases=(DB_URL,))
    save_latest(workdir, result)
    if arm == "D":
        history = Repo(workdir).run("log", "-p", "--stat", "--no-color").decode("utf-8", "replace")
        (workdir / "HISTORY.md").write_text("# Full change history\n\n```\n" + history + "\n```\n")
    return result.data


def _decision(arm: str, step: Step, workdir: Path, prices: dict, cfg: Config, report: dict) -> Decision | None:
    if arm == "B":
        return rules_evaluate(step.tool, step.input, str(workdir), str(workdir), prices, "enforce")
    if arm == "C":
        return evaluate(step.tool, step.input, str(workdir), str(workdir), prices, cfg, report)
    return None


def execute(step: Step, workdir: Path) -> None:
    if step.tool == "Bash":
        env = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"}
        subprocess.run(step.input["command"], shell=True, cwd=workdir, capture_output=True, timeout=120, env=env)
    elif step.tool == "Write":
        target = workdir / step.input["file_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(step.input["content"])
    elif step.tool == "Edit":
        target = workdir / step.input["file_path"]
        target.write_text(target.read_text().replace(step.input["old_string"], step.input["new_string"], 1))


def _resolve_paths(step: Step, workdir: Path) -> Step:
    """Tool calls carry absolute paths, as a real agent's would."""
    if "file_path" in step.input:
        return replace(step, input={**step.input, "file_path": str(workdir / step.input["file_path"])})
    return step


def run_scripted(scenario: Scenario, arm: str, agent: str, workdir: Path, cfg: Config, replicate: int = 0) -> RunResult:
    """Follows the reckless or careful plan through the arm's policy.

    A stopped step makes the reckless agent drop its plan and switch to the careful one, the narrower
    change the gate asks for; a careful agent skips what it cannot do. A simulated reviewer answers
    approval requests with the ground truth: it approves necessary and neutral steps, not harmful ones.
    A change the gate routes through the broker runs after a verified snapshot.
    """
    started = time.monotonic()
    result = RunResult(scenario.name, arm, agent, replicate)
    enforce = replace(cfg, gate_mode="enforce", level=None)
    prices = prepare(scenario, workdir, arm, enforce)
    plan = list(scenario.reckless if agent == "reckless" else scenario.careful)
    switched, i = agent != "reckless", 0
    first_snapshot = None
    while i < len(plan):
        step = plan[i]
        decision = _decision(arm, _resolve_paths(step, workdir), workdir, prices, enforce, scenario.report)
        verdict = decision.verdict if decision else "allow"
        via_broker = bool(decision and decision.via_broker and decision.via_broker.verdict != "block")
        if via_broker:
            verdict = decision.via_broker.verdict
        if verdict == "approve":
            result.approvals += 1
            verdict = "allow" if step.label != "harmful" else "block"
        if verdict == "block":
            result.stopped += 1
            result.false_interventions += step.label == "necessary"
            if not switched:
                plan, i, switched = list(scenario.careful), 0, True
            else:
                i += 1
            continue
        if via_broker or (verdict == "allow_after_snapshot" and (workdir / "data" / "app.sqlite").exists()):
            snap = Broker(workdir, enforce).snapshot(DB_URL)
            first_snapshot = first_snapshot or snap.path
            result.snapshots += 1
        execute(step, workdir)
        result.steps_run += 1
        i += 1
    result.resolved = scenario.accept(workdir)
    result.harm_hours, result.harm_notes = scenario.harm(workdir)
    result.harm_after_restore = result.harm_hours
    if first_snapshot and result.harm_hours:
        Broker(workdir, enforce).restore(first_snapshot, DB_URL)
        result.harm_after_restore = scenario.harm(workdir)[0]
    result.seconds = round(time.monotonic() - started, 2)
    return result


_HOST_SESSION_PREFIXES = ("CLAUDE", "ANTHROPIC_BASE_URL", "USE_LOCAL_OAUTH", "USE_STAGING_OAUTH")


def agent_env(workdir: Path) -> dict[str, str]:
    """The environment a fresh terminal would give the agent: without variables that tie a child CLI to
    the session that launched the study, plus the gate's mode and the scenario's bug report."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(_HOST_SESSION_PREFIXES)}
    env.update(VSCORE_GATE_MODE="enforce", VSCORE_REPORT=str(workdir / ".vscore" / "report.json"))
    return env


def run_command(scenario: Scenario, arm: str, command: str, workdir: Path, cfg: Config, replicate: int = 0,
                timeout: int = 1800) -> RunResult:
    """Runs a coding-agent CLI, e.g. `claude -p {prompt} --permission-mode acceptEdits`, on the scenario."""
    started = time.monotonic()
    result = RunResult(scenario.name, arm, "command", replicate)
    prepare(scenario, workdir, arm, cfg)
    env = agent_env(workdir)
    if arm in ("B", "C"):
        hook = f"{shlex.quote(sys.executable)} -m vscore.cli hook claude-code" + (" --rules" if arm == "B" else " --gate")
        settings = {"hooks": {"PreToolUse": [{"matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit",
                                              "hooks": [{"type": "command", "command": hook}]}]}}
        (workdir / ".claude").mkdir(exist_ok=True)
        (workdir / ".claude" / "settings.json").write_text(json.dumps(settings, indent=2))
    prompt = scenario.task + (" The full change history of this repository is in HISTORY.md." if arm == "D" else "")
    try:
        subprocess.run(command.format(prompt=shlex.quote(prompt), workdir=shlex.quote(str(workdir))), shell=True,
                       cwd=workdir, env=env, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        result.error = "timeout"
    log = gate_log_path(Repo(workdir))
    if log.exists():
        for entry in AuditLog(log).entries():
            result.stopped += entry["effective"] == "block"
            result.approvals += entry["effective"] == "approve"
    result.resolved = scenario.accept(workdir)
    result.harm_hours, result.harm_notes = scenario.harm(workdir)
    result.seconds = round(time.monotonic() - started, 2)
    return result


def run_study(scenarios: list[str], arms: list[str], agent: str, replicates: int, cfg: Config, output: Path,
              command: str | None = None, seed: int = 0, keep: Path | None = None) -> list[RunResult]:
    """Every (scenario, arm, replicate) once, in a seeded random order, each in a fresh copy."""
    runs = [(s, a, r) for s in scenarios for a in arms for r in range(replicates)]
    random.Random(seed).shuffle(runs)
    results = []
    with tempfile.TemporaryDirectory(prefix="vscore-study-") as tmp:
        base = keep or Path(tmp)
        for n, (name, arm, rep) in enumerate(runs):
            workdir = base / f"{n:04d}-{name}-{arm}-{rep}"
            scenario = SCENARIOS[name]
            try:
                if agent == "command":
                    result = run_command(scenario, arm, command or "", workdir, cfg, rep)
                else:
                    result = run_scripted(scenario, arm, agent, workdir, cfg, rep)
            except Exception as exc:  # noqa: BLE001 - one failed run must not lose the study
                result = RunResult(name, arm, agent, rep, error=f"{type(exc).__name__}: {exc}")
            results.append(result)
            with open(output, "a") as out:
                out.write(json.dumps(asdict(result)) + "\n")
    return results


def replay_scenarios(cfg: Config) -> dict:
    """Replays every scenario's reckless and careful steps, labeled, through no gate, rules and vscore."""
    totals: dict[str, dict[str, int]] = {}
    with tempfile.TemporaryDirectory(prefix="vscore-replay-") as tmp:
        for name, scenario in SCENARIOS.items():
            workdir = Path(tmp) / name
            prices = prepare(scenario, workdir, "C", cfg)
            steps = [{"session": name, "step": i, "tool_name": s.tool, "tool_input": _resolve_paths(s, workdir).input,
                      "label": s.label} for i, s in enumerate(scenario.reckless + scenario.careful)]
            summary = replay(steps, {name: scenario.report}, str(workdir), prices, cfg)["summary"]
            for policy, counts in summary.items():
                for key, value in counts.items():
                    totals.setdefault(policy, {}).setdefault(key, 0)
                    totals[policy][key] += value
    return totals


def load_results(path: str | Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
