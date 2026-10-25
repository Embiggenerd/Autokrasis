# vscore usage

vscore prices every file, function and database table in a repository in engineering-hours of expected loss, and gates coding-agent actions against those prices. The design is in [plan.md](plan.md).

## Install

Python 3.14 or newer (the standard library's `compression.zstd` is used; there are no runtime dependencies).

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

The scenario benchmark also needs the `sqlite3` command-line client.

## Give it to any agent

Tell the agent: "Use the vscore skill in `skills/vscore/SKILL.md`." The skill tells it to run
`vscore compare` before every destructive step and to stop on STOP. It works with any agent that can
read a file and run a command, in any harness. Harnesses that load skills natively (such as Claude
Code) can also install the `skills/vscore` folder as a skill.

## The simple check: is this action proportionate to the bug?

`vscore compare` compares two numbers: the W (surviving compressed work, in bytes) of the code a bug is about, and the W an action would destroy. An action that destroys more than twice what the bug is worth is stopped:

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

Losses W cannot weigh against code are stopped outright: dropping or emptying a database table, rewriting Git history, and removing files named by a command vscore cannot read. A bug in a tiny or new file still allows changes up to W 500 (`compare_floor_w`). The limit is `--limit` or `compare_limit`.

The bug is given with `--bug`, which takes files, directories, globs or `path::function` and can repeat. Otherwise it comes from `VSCORE_BUG` (comma-separated), or from the `nodes` of `.vscore/report.json`.

As a Claude Code hook, the check is the default:

```bash
vscore hook settings            # prints the settings.json snippet: `vscore hook claude-code`
```

The hook is silent when an action is proportionate to the bug. When it isn't, it denies the call with both numbers, so the agent can make a narrower fix. Its first call in a repository reads the scan (seconds on a large repository); later calls take about 0.25 s. The rest of this page describes the priced gate (`vscore hook claude-code --gate`): hours, budgets, the broker and approvals. It does more, and asks more of you.

## Score and price

```bash
vscore scan <repo> -o scan.json        # W, D and V points per file and per Python symbol, plus the codebase total
vscore show scan.json --top 20         # highest V first
vscore explain <repo> path/to/file.py  # one file's breakdown and the commits behind its W
vscore price <repo> --db sqlite:///data/app.sqlite -o prices.json   # hours and bands for files, symbols, tables
```

`price` saves its result to `.git/vscore-cache/prices/latest.json`, where the gate and hooks read it. When the saved prices were computed at another commit or with another formula, the gate recomputes them before deciding (keeping their databases and calibration) and records `"freshness": "repriced (prices_stale: …)"` in the decision; re-run `price` after data changes.

Read the units carefully:

- **V points** (`scan`) are measured: dimensionless, from history and imports.
- **Hours** (`price`) are modeled: V's components converted with fixed priors in the configuration.
- **Calibrated hours** (`price --calibration`) are fitted to the sandbox check's loss labels, which are a proxy (a restore allowance plus failing tests times a fixed rate). They are proxy hours, not measured engineering-hours.

Bands built on hours are therefore provisional.

Everything is deterministic for a given revision, configuration and tool versions; each output records them.

## Add evidence

Events go into an append-only store in `.git/vscore-cache/events.sqlite`. Git history is ingested by `price` automatically; the other sources read local files:

```bash
vscore fetch-github owner/name -o prs.json   # uses your GitHub CLI login
vscore ingest review <repo> prs.json
vscore ingest ci <repo> runs.jsonl           # {"run_id","sha","at","protected","conclusion"} per line
vscore ingest incidents <repo> incidents.jsonl   # {"id","at","severity":1-4,"paths":[],"commits":[]}
vscore ingest traffic <repo> traffic.jsonl   # {"path","at","requests","errors"}
vscore broker verify-restore sqlite:///data/app.sqlite --repo <repo>   # records a tested restore
```

Work from commits that a CI or review source saw but never verified counts at half weight. A tested restore younger than seven days lowers a table's price from a rebuild to a restore.

## Check prices against measured loss

```bash
vscore check <repo> --labels labels.csv      # deletes each code file in a sandbox, runs the tests, compares
vscore calibrate labels.csv -o calibration.json
vscore price <repo> --calibration calibration.json
```

`check` reports Spearman correlations of each measure with measured loss (ablations), the miss rate and band accuracy.

## Gate agent actions

A bug report sets the budget. Put it in `.vscore/report.json` or point `VSCORE_REPORT` at it:

```json
{"id": "BUG-12", "impact": "degraded", "users_affected": 40, "nodes": ["file:app/billing.py"],
 "evidence": [{"kind": "failing_ci_test", "ref": "test_invoice_total"}]}
```

The full budget covers only the listed nodes; anything else gets the default session budget (1 h). Evidence kinds, strongest first: `reproduced_by_harness`, `failing_ci_test`, `telemetry`, `independent_report`. An `agent_claim` counts for nothing, and a report with no independent check is capped at 10 h.

```bash
vscore gate <repo> --command "sqlite3 data/app.sqlite 'DROP TABLE customers'" --report bug.json --enforce
vscore gate <repo> --tool Edit --input '{"file_path": "app/pricing.py", "old_string": "…", "new_string": "…"}'
```

The decision record is printed as JSON. `outcome` is what happens: `allow` (runs silently), `warn` (runs, with the price shown), `ask` or `deny`; `revision` and `freshness` say which prices it used.

**Conformance.** `vscore/conformance.py` lists the cases every door into the gate must agree on (the benchmark scenarios' steps, the first study's false alarms, a harmful neighbor of each, edge cases, freshness and failure cases), with the outcome enforce mode must give. `tests/test_conformance.py` runs each through the library, the CLI and the Claude Code hook and checks they return the same record. Cases whose behavior is not built yet are marked pending and run as expected failures.

### Claude Code hook

```bash
vscore hook settings            # prints the settings.json snippet
```

Add the snippet to the project's `.claude/settings.json`. The hook never grants permission: harmless calls get no output, so the normal permission flow applies. In warn mode (the default) it shows a message with the price; in enforce mode it asks for approval or denies with the reason. Switch modes in `.vscore/config.toml` (or the file `VSCORE_CONFIG` names):

```toml
[vscore]
gate_mode = "enforce"
```

or with `VSCORE_GATE_MODE=enforce`. Every decision is appended to `.git/vscore-cache/gate.jsonl`, a hash-chained log.

For finer control, set an enforcement level instead (`level = 0` to `3`, or `VSCORE_LEVEL`). Level 0 only logs, level 1 is warn mode, and level 2 is enforce mode. Level 3 decides as 2 does and is meant for deployments where the broker and database grants hold the destructive rights. When the gate cannot decide normally, the failure table in the integration plan decides, and the record's `error` names the row:

- **Missing or stale prices:** pricing starts in the background. At levels 2 and 3 the gate waits up to `time_limit_s` (default 2 s), then denies destructive-looking actions until pricing finishes.
- **A script whose arguments name priced files or databases:** asks first.
- **A malformed tool call:** denied.
- **A hook whose core crashes or misses the time limit:** denies destructive-looking actions.

At levels 0 and 1 each of these warns or logs instead. List database URLs under `databases = [...]` to have them priced when no saved prices name them.

Hooks are advisory: a shell can go around them. The guarantee is resource permissions plus the broker.

### Approvals

When the hook asks, as with a script whose arguments name the database, its answer includes a decision id. In Claude Code's interactive mode the person approves at the prompt as usual. For an agent nobody is watching, a person approves the ask from a shell, and the agent's retry of the same call then runs:

```bash
vscore approvals .                       # asks waiting for a person
vscore approve . 3f9c2a7d1e8b4c60 --by alice
```

An approval allows one execution of that exact tool call. It expires after `approval_ttl_s` (default 15 minutes), and it stops applying if anything the action touches changes after the decision: a file's contents, a SQLite table's schema or row count, or for Git targets the commit and working-tree status. The hook denies an agent's own `vscore approve`, and any write under `.git/vscore-cache`. That protection is advisory, like the hook itself. Approvals an agent cannot forge need signatures, which the Buzz integration adds.

### Broker

The broker is the only holder of rights to destroy priced data. It snapshots, verifies the snapshot, prices the change as restorable, asks the gate in enforce mode, then runs it. A change that stays irreversible even with the snapshot, such as deleting rows matching a `WHERE` clause, is refused with a decision id. Once a person runs `vscore approve` for it, the same command runs once:

```bash
vscore broker sql sqlite:///data/app.sqlite "DELETE FROM orders WHERE status = 'void'" --repo . --report bug.json
vscore broker restore sqlite:///data/app.sqlite .git/vscore-cache/snapshots/<file>.sqlite --repo .
vscore broker pg-role agent_role --repo .   # PostgreSQL grants: no DROP/TRUNCATE, no DELETE on band 3-4 tables
```

PostgreSQL snapshots need `pg_dump` and `pg_restore` on PATH.

### Reviews and replays

```bash
vscore diff-check <repo> --base origin/main            # exits 1 if a band 3-4 file is deleted or mostly rewritten
vscore replay <repo> sessions.jsonl                    # labeled steps through no gate, rules and vscore
```

## Benchmark and agent study

```bash
vscore bench list
vscore bench build failed_migration -o /tmp/scenario     # inspect one scenario
vscore bench run all --agent reckless --replicates 1 -o study.jsonl    # instrumentation run, scripted agent
vscore bench run all --agent command --command "claude -p {prompt} --permission-mode acceptEdits" --replicates 3 -o study.jsonl
vscore bench report study.jsonl -o report.md
vscore bench replay
```

Arms: A control, B rules baseline, C priced gate, D raw history in `HISTORY.md`. For a command agent, arms B and C install the hook in the scenario's `.claude/settings.json` in enforce mode. A real-agent run spends model credits; scripted runs do not.

## Configuration

All settings live in `vscore/config.py` and can be overridden in a TOML file passed with `--config` (or `.vscore/config.toml` for hooks). The configuration hash is recorded in every output.
