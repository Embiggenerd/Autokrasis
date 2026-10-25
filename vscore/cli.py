"""Command line: vscore scan | show | explain | compare | hook."""

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


def _total_text(total: dict) -> str:
    if total["W"] is None:
        return f"total W and V null ({total['reason']}): nothing measured"
    partial = (f", partial: {total['files_measured']} of {total['files_eligible']} files measured"
               if total["partial"] else "")
    return f"total W {total['W']:.0f}, V {total['V']:.2f}{partial}"


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
        f"in {time.monotonic() - start:.1f}s; {_total_text(data['total'])}",
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
    print(f"\ncodebase {_total_text(total)}")
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


def cmd_hook(args) -> int:
    from vscore.compare import hook, settings_snippet

    if args.target == "settings":
        sys.stdout.write(settings_snippet())
        return 0
    answer = hook(json.load(sys.stdin))
    if answer is not None:
        sys.stdout.write(json.dumps(answer))
    return 0


def cmd_compare(args) -> int:
    from vscore.compare import bug_for, compare

    cfg = Config.load(args.config)
    if args.limit is not None:
        from dataclasses import replace

        cfg = replace(cfg, compare_limit=args.limit)
    tool_input = json.loads(args.input) if args.input else {"command": args.command or ""}
    bug = args.bug if args.bug is not None else bug_for(args.repo)
    result = compare(args.repo, bug, args.tool, tool_input, args.cwd, cfg)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(f"bug       W {result.bug_w:>12,.0f}  {', '.join(result.bug) or '(none named)'}")
        print(f"destroys  W {result.destroyed:>12,.0f}  {result.summary}")
        for path, w in result.files[:10]:
            print(f"            {w:>12,.0f}  {path}")
        for loss in result.unmeasured:
            print(f"            {'unmeasured':>12}  {loss}")
        print(f"codebase  W {result.codebase:>12,.0f}")
        verdict = "RUN" if result.allowed else "STOP"
        print(f"\n{result.ratio:.1f}x the bug (limit {result.limit:g}x): {verdict}")
    return 0 if result.allowed else 4


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

    p = sub.add_parser("compare", help="compare the W of a bug with the W an action would destroy; exit 4 to stop")
    p.add_argument("repo")
    p.add_argument("--bug", action="append", help="a file, directory, glob or path::symbol the bug is about; "
                   "repeatable (default: VSCORE_BUG, or the nodes of .vscore/report.json)")
    p.add_argument("--command", help="the shell command the agent wants to run")
    p.add_argument("--tool", default="Bash", help="the tool, for --input (Bash, Write, Edit, MultiEdit)")
    p.add_argument("--input", help="the tool call's input as JSON, in Claude Code's format")
    p.add_argument("--cwd")
    p.add_argument("--limit", type=float, help="how many times the bug's W an action may destroy (default 2)")
    p.add_argument("--config")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("hook", help="Claude Code hook: `hook claude-code` reads a PreToolUse payload on stdin; "
                       "`hook settings` prints the settings.json entry")
    p.add_argument("target", choices=("claude-code", "settings"))
    p.set_defaults(func=cmd_hook)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
