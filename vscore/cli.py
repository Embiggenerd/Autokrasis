"""Command line: vscore scan | show | explain | check."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from vscore.config import Config
from vscore.scan import scan


def _fmt(value, width: int, digits: int = 1) -> str:
    if value is None:
        return "-".rjust(width)
    if isinstance(value, float):
        return f"{value:.{digits}f}".rjust(width)
    return str(value).rjust(width)


def _sort_key(field: str):
    return lambda f: (f[field] is None, -(f[field] or 0), f["path"])


def cmd_scan(args) -> int:
    start = time.monotonic()
    result = scan(args.repo, args.rev, Config.load(args.config), args.allow_shallow, args.jobs)
    output = result.to_json()
    if args.output:
        Path(args.output).write_text(output)
    else:
        sys.stdout.write(output)
    data = result.data
    cov = data["scan"]["coverage"]
    print(
        f"scanned {cov['eligible']} eligible files ({cov['excluded']} excluded) at {data['scan']['revision'][:12]} "
        f"in {time.monotonic() - start:.1f}s; total W {data['total']['W']:.0f}, V {data['total']['V']:.2f}",
        file=sys.stderr,
    )
    return 0


def cmd_show(args) -> int:
    data = json.loads(Path(args.scan_file).read_text())
    files = sorted(data["files"], key=_sort_key(args.sort))[: args.top]
    print(f"{'V':>7} {'W':>10} {'D':>5} {'commits':>7}  {'class':<11} {'conf':<7} path")
    for f in files:
        print(f"{_fmt(f['V'], 7, 2)} {_fmt(f['W'], 10, 0)} {_fmt(f['D'], 5)} {_fmt(f['commits'], 7)}  "
              f"{f['class']:<11} {f['confidence']:<7} {f['path']}")
    total = data["total"]
    print(f"\ncodebase total: W {total['W']:.0f}, V {total['V']:.2f}")
    return 0


def cmd_explain(args) -> int:
    result = scan(args.repo, args.rev, Config.load(args.config), args.allow_shallow, args.jobs, only={args.path})
    records = result.data["files"]
    if not records:
        excluded = {e["path"]: e["reason"] for e in result.data["excluded"]}
        reason = excluded.get(args.path, "not tracked at this revision")
        print(f"{args.path}: not scored ({reason})")
        return 1
    f = records[0]
    print(f"{f['path']}  [{f['class']}, confidence {f['confidence']}]")
    print(f"  V = {_fmt(f['V'], 0, 4)}   W = {_fmt(f['W'], 0, 1)} bytes   D = {_fmt(f['D'], 0)}")
    for part, reason in sorted(f["reasons"].items()):
        print(f"  {part} is null: {reason}")
    importers = result.importers.get(f["path"], [])
    if importers:
        print(f"  imported directly by {len(importers)}: {', '.join(importers[:8])}{' ...' if len(importers) > 8 else ''}")
    contributions = sorted(result.contributions.get(f["path"]) or [], key=lambda c: (-c.work, c.commit))
    if contributions:
        print(f"  top commits by surviving work ({f['commits']} contributed):")
        print(f"    {'commit':<12} {'info':>7} {'added':>6} {'alive':>6} {'surv':>5} {'work':>8}  path then")
        for c in contributions[: args.commits]:
            print(f"    {c.commit[:12]:<12} {c.info:>7} {c.added_lines:>6} {c.surviving_lines:>6} "
                  f"{c.surv:>5.2f} {c.work:>8.1f}  {c.path}")
    return 0


def _load_json(path: str | None) -> dict | None:
    return json.loads(Path(path).read_text()) if path else None


def cmd_check(args) -> int:
    from vscore.harness.perturb import band_metrics, correlations, perturb, write_labels

    cases, baseline = perturb(args.repo, args.rev, Config.load(args.config), calibration=_load_json(args.calibration))
    print(f"{baseline} tests pass before any deletion\n")
    print(f"{'failing':>7} {'loss h':>7} {'price h':>8} {'band':>4} {'V':>7} {'W':>9} {'D':>4} {'churn':>6} {'size':>5}  path")
    for c in sorted(cases, key=lambda c: (-c.failing, c.path)):
        print(f"{c.failing:>7} {c.measured_hours:>7.1f} {_fmt(c.price, 8, 1)} {c.band:>4} {_fmt(c.V, 7, 2)} "
              f"{_fmt(c.W, 9, 0)} {_fmt(c.D, 4)} {c.churn:>6} {c.size:>5}  {c.path}")
    print("\nSpearman correlation with measured loss (ablations: each measure alone):")
    for measure, rho in correlations(cases).items():
        print(f"  {measure:<13} {'n/a' if rho is None else f'{rho:+.3f}'}")
    metrics = band_metrics(cases)
    miss = "n/a (no high-loss files)" if metrics["miss_rate"] is None else f"{metrics['miss_rate']:.0%}"
    print(f"\nmiss rate {miss}; band accuracy {metrics['band_accuracy']:.0%} over {len(cases)} files")
    if args.labels:
        write_labels(cases, args.labels)
        print(f"wrote calibration labels to {args.labels}")
    return 0


def cmd_ingest(args) -> int:
    from vscore.adapters import external
    from vscore.adapters.gitlog import git_events
    from vscore.events import ActorHasher, EventStore
    from vscore.gitrepo import Repo

    repo = Repo(args.repo)
    actors = ActorHasher(repo.git_dir / "vscore-cache" / "actor.key")
    if args.source != "git" and not args.file:
        print(f"ingest {args.source} needs a file", file=sys.stderr)
        return 2
    events = {
        "git": lambda: git_events(repo, repo.resolve(args.rev), actors),
        "ci": lambda: external.ci_events(args.file),
        "review": lambda: external.review_events(args.file, actors),
        "incidents": lambda: external.incident_events(args.file),
        "traffic": lambda: external.traffic_events(args.file),
    }[args.source]()
    store = EventStore.for_repo(repo.git_dir)
    added = store.add(events)
    print(f"{added} new {args.source} events ({len(events) - added} already stored); store has {store.count()}")
    store.close()
    return 0


def cmd_fetch_github(args) -> int:
    from vscore.adapters.external import fetch_github_prs

    count = fetch_github_prs(args.slug, args.limit, args.output)
    print(f"wrote {count} merged pull requests to {args.output}; next: vscore ingest review <repo> {args.output}")
    return 0


def cmd_price(args) -> int:
    from vscore.prices.run import price_repo, save_latest

    start = time.monotonic()
    result = price_repo(args.repo, args.rev, Config.load(args.config), tuple(args.db or ()),
                        _load_json(args.calibration), args.jobs, allow_shallow=args.allow_shallow)
    if args.output:
        Path(args.output).write_text(result.to_json())
    latest = save_latest(args.repo, result)
    nodes = sorted(result.data["nodes"], key=lambda n: (-n["band"], -(n["hours"] or 0), n["node"]))
    print(f"{'band':>4} {'hours':>9} {'at risk':>9} {'default':>7}  {'class':<11} node")
    for n in nodes[: args.top]:
        print(f"{n['band']:>4} {_fmt(n['hours'], 9, 1)} {_fmt(n['at_risk_hours'], 9, 1)} {n['default_band']:>7}  "
              f"{n['class']:<11} {n['node']}")
    bands = [sum(n["band"] == b for n in result.data["nodes"]) for b in range(5)]
    meta = result.data["prices"]
    print(f"\n{len(nodes)} nodes by band 0-4: {bands}; calibrated: {meta['calibrated']}; events: {meta['events']}")
    print(f"priced in {time.monotonic() - start:.1f}s; saved to {latest}", file=sys.stderr)
    return 0


def cmd_calibrate(args) -> int:
    from vscore.harness.perturb import read_labels
    from vscore.prices.model import fit_calibration

    fit = fit_calibration(read_labels(args.labels))
    fit["formula"] = __import__("vscore").FORMULA_VERSION
    Path(args.output).write_text(json.dumps(fit, indent=2, sort_keys=True) + "\n")
    print(f"fitted {fit['n']} files: ln(hours) = {fit['b0']:.3f} + {fit['b1']:.3f}*log2(1+W) + "
          f"{fit['b2']:.3f}*log2(1+D); R^2 {fit['r2']:.2f}, residual sd {fit['residual_sd']:.2f}")
    return 0


def _prices_for(repo: str, cfg: Config, prices_file: str | None = None) -> dict:
    from vscore.prices.run import load_latest, price_repo, save_latest

    if prices_file:
        return json.loads(Path(prices_file).read_text())
    if (latest := load_latest(repo)) is not None:
        return latest
    result = price_repo(repo, cfg=cfg)
    save_latest(repo, result)
    return result.data


def cmd_gate(args) -> int:
    from dataclasses import replace

    from vscore.budget import load_report
    from vscore.gate import evaluate

    cfg = Config.load(args.config)
    if args.enforce:
        cfg = replace(cfg, gate_mode="enforce")
    tool_input = {"command": args.command} if args.tool == "Bash" else {"file_path": args.file, "content": args.content or ""}
    decision = evaluate(args.tool, tool_input, args.repo, args.cwd or args.repo, _prices_for(args.repo, cfg, args.prices),
                        cfg, load_report(args.report))
    print(json.dumps(decision.to_dict(), indent=2))
    return {"allow": 0, "allow_after_snapshot": 0, "warn": 0, "approve": 3, "block": 4}[decision.effective]


def cmd_hook(args) -> int:
    from vscore.hooks import claude_code, settings_snippet

    if args.target == "settings":
        sys.stdout.write(settings_snippet("rules" if args.rules else "vscore"))
        return 0
    answer = claude_code(json.load(sys.stdin), "rules" if args.rules else "vscore")
    if answer is not None:
        sys.stdout.write(json.dumps(answer))
    return 0


def cmd_broker(args) -> int:
    from vscore.broker import Broker, BrokerRefused, postgres_agent_role_sql
    from vscore.budget import load_report

    cfg = Config.load(args.config)
    broker = Broker(args.repo, cfg)
    try:
        if args.op == "sql":
            entry = broker.run_sql(args.database, args.sql, _prices_for(args.repo, cfg), load_report(args.report),
                                   args.approve)
            print(f"ran after snapshot {entry['snapshot']}: {entry['decision']['verdict']}, "
                  f"{entry['decision']['hours']} h within {entry['decision']['budget_hours']} h budget")
        elif args.op == "verify-restore":
            snap = broker.verify_restore(args.database)
            print(f"restore tested from {snap.path} ({sum(snap.rows.values())} rows in {len(snap.rows)} tables); "
                  "re-run `vscore price` to lower table prices")
        elif args.op == "restore":
            broker.restore(args.sql, args.database)
            print(f"restored {args.database} from {args.sql}")
        elif args.op == "pg-role":
            sys.stdout.write(postgres_agent_role_sql(args.database, _prices_for(args.repo, cfg)))
    except BrokerRefused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 4
    return 0


def cmd_diff_check(args) -> int:
    from vscore.diffcheck import diff_check
    from vscore.prices.run import price_repo

    prices = price_repo(args.repo, args.base, Config.load(args.config)).data
    findings = diff_check(args.repo, args.base, prices, args.head, args.min_band)
    for f in findings:
        print(f"band {f['band']}: {f['change']} {f['path']}")
    if findings:
        print(f"{len(findings)} high-value files deleted or rewritten; review before merging", file=sys.stderr)
    return 1 if findings else 0


def cmd_replay(args) -> int:
    from vscore.replay import load_sessions, replay

    cfg = Config.load(args.config)
    steps, reports = load_sessions(args.sessions)
    result = replay(steps, reports, args.repo, _prices_for(args.repo, cfg, args.prices), cfg)
    print(f"{'policy':<8} {'harmful':>7} {'stopped':>7} {'contained':>9} {'missed':>6} {'necessary':>9} "
          f"{'false int.':>10} {'approvals':>9}")
    for policy, s in result["summary"].items():
        print(f"{policy:<8} {s['harmful']:>7} {s['stopped']:>7} {s['contained']:>9} {s['missed']:>6} "
              f"{s['necessary']:>9} {s['false_interventions']:>10} {s['approvals']:>9}")
    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    return 0


def cmd_bench(args) -> int:
    from vscore.bench import report as bench_report
    from vscore.bench import study
    from vscore.bench.scenarios import SCENARIOS

    cfg = Config.load(args.config)
    if args.op == "list":
        for s in SCENARIOS.values():
            print(f"{s.name:<22} {s.title}: tempting '{s.tempting}', truth '{s.truth}'")
    elif args.op == "build":
        target = Path(args.output or args.scenario)
        study.prepare(SCENARIOS[args.scenario], target, args.arms or "C", cfg)
        print(f"built {args.scenario} at {target}; task:\n{SCENARIOS[args.scenario].task}")
    elif args.op == "run":
        names = list(SCENARIOS) if args.scenario in (None, "all") else args.scenario.split(",")
        arms = (args.arms or "A,B,C,D").split(",")
        if args.agent == "command" and not args.command:
            print("--agent command needs --command, e.g. \"claude -p {prompt} --permission-mode acceptEdits\"", file=sys.stderr)
            return 2
        output = Path(args.output or "study.jsonl")
        results = study.run_study(names, arms, args.agent, args.replicates, cfg, output, args.command, args.seed,
                                  Path(args.keep) if args.keep else None)
        print(f"{len(results)} runs appended to {output}; next: vscore bench report {output}")
    elif args.op == "report":
        text = bench_report.markdown(study.load_results(args.scenario))
        if args.output:
            Path(args.output).write_text(text)
        print(text)
    elif args.op == "replay":
        totals = study.replay_scenarios(cfg)
        print(f"{'policy':<8} {'harmful':>7} {'stopped':>7} {'contained':>9} {'missed':>6} {'necessary':>9} "
              f"{'false int.':>10} {'approvals':>9}")
        for policy, s in totals.items():
            print(f"{policy:<8} {s['harmful']:>7} {s['stopped']:>7} {s['contained']:>9} {s['missed']:>6} "
                  f"{s['necessary']:>9} {s['false_interventions']:>10} {s['approvals']:>9}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vscore", description="V points for the files of a Git repository.")
    sub = parser.add_subparsers(dest="command", required=True)

    def repo_args(p):
        p.add_argument("repo", help="path to a Git repository")
        p.add_argument("--rev", default="HEAD", help="revision to scan (resolved to a full SHA)")
        p.add_argument("--config", help="TOML file with a [vscore] table of overrides")
        p.add_argument("--allow-shallow", action="store_true", help="score a shallow clone, with partial confidence")
        p.add_argument("--jobs", type=int, help="parallel git processes")

    p = sub.add_parser("scan", help="score every eligible file and write JSON")
    repo_args(p)
    p.add_argument("-o", "--output", help="write JSON here instead of stdout")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("show", help="list files from a scan, highest first")
    p.add_argument("scan_file")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--sort", choices=("V", "W", "D"), default="V")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("explain", help="show one file's breakdown and the commits behind its W")
    repo_args(p)
    p.add_argument("path")
    p.add_argument("--commits", type=int, default=10)
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("check", help="delete files one at a time in a sandbox and compare prices with the loss")
    p.add_argument("repo")
    p.add_argument("--rev", default="HEAD")
    p.add_argument("--config")
    p.add_argument("--calibration", help="calibration.json from `vscore calibrate`")
    p.add_argument("--labels", help="write measured-loss labels (CSV) for `vscore calibrate`")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("ingest", help="add events from a source to the repository's event store")
    p.add_argument("source", choices=("git", "ci", "review", "incidents", "traffic"))
    p.add_argument("repo")
    p.add_argument("file", nargs="?", help="input file (all sources except git)")
    p.add_argument("--rev", default="HEAD")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("fetch-github", help="export merged pull requests with the GitHub CLI, for `ingest review`")
    p.add_argument("slug", help="owner/name")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--limit", type=int, default=500)
    p.set_defaults(func=cmd_fetch_github)

    p = sub.add_parser("price", help="price every file (and database table) in engineering-hours and bands")
    repo_args(p)
    p.add_argument("--db", action="append", help="database URL (sqlite:///path or postgresql://...); repeatable")
    p.add_argument("--calibration", help="calibration.json from `vscore calibrate`")
    p.add_argument("-o", "--output", help="also write the prices JSON here")
    p.add_argument("--top", type=int, default=25)
    p.set_defaults(func=cmd_price)

    p = sub.add_parser("calibrate", help="fit hours to measured-loss labels from `vscore check --labels`")
    p.add_argument("labels")
    p.add_argument("-o", "--output", required=True)
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("gate", help="decide one proposed action against the prices and a bug report")
    p.add_argument("repo")
    p.add_argument("--tool", default="Bash", choices=("Bash", "Write"))
    p.add_argument("--command", help="shell command, for --tool Bash")
    p.add_argument("--file", help="file path, for --tool Write")
    p.add_argument("--content", help="new file content, for --tool Write")
    p.add_argument("--cwd")
    p.add_argument("--report", help="bug report JSON")
    p.add_argument("--prices", help="prices JSON (default: the newest saved prices)")
    p.add_argument("--config")
    p.add_argument("--enforce", action="store_true", help="decide as in enforce mode")
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser("hook", help="agent hooks: `hook claude-code` reads a PreToolUse payload on stdin")
    p.add_argument("target", choices=("claude-code", "settings"))
    p.add_argument("--rules", action="store_true", help="use the rules baseline instead of prices")
    p.set_defaults(func=cmd_hook)

    p = sub.add_parser("broker", help="run destructive SQL after a verified snapshot, or test restores")
    p.add_argument("op", choices=("sql", "verify-restore", "restore", "pg-role"))
    p.add_argument("database", help="database URL (for pg-role: the role name)")
    p.add_argument("sql", nargs="?", help="SQL to run (for restore: the snapshot path)")
    p.add_argument("--repo", default=".")
    p.add_argument("--report", help="bug report JSON that backs the change")
    p.add_argument("--approve", action="store_true", help="a human approved this irreversible change")
    p.add_argument("--config")
    p.set_defaults(func=cmd_broker)

    p = sub.add_parser("diff-check", help="fail when a change deletes or rewrites band-3 or band-4 files")
    p.add_argument("repo")
    p.add_argument("--base", required=True)
    p.add_argument("--head", help="default: the working tree")
    p.add_argument("--min-band", type=int, default=3)
    p.add_argument("--config")
    p.set_defaults(func=cmd_diff_check)

    p = sub.add_parser("replay", help="replay labeled agent sessions through no gate, rules, and vscore")
    p.add_argument("repo")
    p.add_argument("sessions", help="JSON lines of labeled steps")
    p.add_argument("--prices")
    p.add_argument("--config")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("bench", help="benchmark scenarios and the four-arm agent study")
    p.add_argument("op", choices=("list", "build", "run", "report", "replay"))
    p.add_argument("scenario", nargs="?", help="scenario name(s), or for report the results file")
    p.add_argument("--arms", help="comma-separated arms A,B,C,D (build: the arm to prepare for)")
    p.add_argument("--agent", choices=("reckless", "careful", "command"), default="reckless")
    p.add_argument("--command", help="agent command template with {prompt} and {workdir}")
    p.add_argument("--replicates", type=int, default=3)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--keep", help="keep each run's working copy under this directory")
    p.add_argument("-o", "--output")
    p.add_argument("--config")
    p.set_defaults(func=cmd_bench)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
