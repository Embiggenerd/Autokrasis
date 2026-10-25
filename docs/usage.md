# vscore usage

vscore measures the work stored in each file and top-level Python function of a Git repository, as W (surviving compressed work, in bytes, from the repository's history), and checks an agent's action against the W of the code a bug is about. How W is measured is in [plan.md](plan.md).

## Install

Python 3.14 or newer (the standard library's `compression.zstd` is used; there are no runtime dependencies).

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

## Give it to any agent

The [README](../README.md) has the three routes: `npx skills add Embiggenerd/Autokrasis` for any
agent, the Claude Code plugin (`claude plugin marketplace add Embiggenerd/Autokrasis`, then
`claude plugin install vscore@autokrasis`), which also installs the hook, or telling an agent in chat
to use the skill at its raw link. The skill is `skills/vscore/SKILL.md`. It tells the agent to run
`vscore compare` before every destructive step and to stop on STOP.

## Compare an action with the bug

`vscore compare` compares two numbers: the W of the code a bug is about, and the W an action would destroy. An action that destroys more than twice what the bug is worth is stopped:

```bash
vscore compare <repo> --bug src/flask/app.py --command "rm -rf src"
```

```
bug       W       14,422  src/flask/app.py
destroys  W       83,730  rm -rf src
                  14,422  src/flask/app.py
                   9,606  src/flask/cli.py
                  ...
codebase  W      329,721

5.8x the bug (limit 2x): STOP
```

What counts as destroyed:

- files that are deleted, overwritten or mostly rewritten, with their uncommitted work
- top-level functions and classes that are removed
- uncommitted work that is discarded

Losses W cannot weigh against code are stopped outright: dropping or emptying a database table, rewriting Git history, and removing files named by a command vscore cannot read. A bug in a tiny or new file still allows changes up to W 500 (`compare_floor_w`). The limit is `--limit` or `compare_limit`. `--json` prints the result as JSON; the exit code is 0 to run and 4 to stop.

The bug is given with `--bug`, which takes files, directories, globs or `path::function` and can repeat. Otherwise it comes from `VSCORE_BUG` (comma-separated), or from the `nodes` of `.vscore/report.json`. A file write or edit is checked with `--tool Write` (or `Edit`) and `--input` holding the tool call's JSON.

## Claude Code hook

```bash
vscore hook settings            # prints the settings.json snippet: `vscore hook claude-code`
```

The hook runs the comparison before every Bash, Write and Edit call. It is silent when an action is proportionate to the bug. When it isn't, it denies the call with both numbers, so the agent can make a narrower fix. Its first call in a repository reads the scan (seconds on a large repository); later calls take about 0.25 s. Each check is appended to `.git/vscore-cache/compare.jsonl`, a hash-chained log.

## Score files

```bash
vscore scan <repo> -o scan.json        # W, D and V points per file and per Python symbol, plus the codebase total
vscore show scan.json --top 20         # highest V first
vscore explain <repo> path/to/file.py  # one file's breakdown and the commits behind its W
```

V points combine W with D, the number of files that import a file directly. Everything is deterministic for a given revision, configuration and tool versions; each output records them.

## Configuration

All settings live in `vscore/config.py` and can be overridden in a TOML file passed with `--config` (or `.vscore/config.toml`, or the file `VSCORE_CONFIG` names, for the hook). The configuration hash is recorded in every scan.
