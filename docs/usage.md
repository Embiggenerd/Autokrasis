# vscore usage

vscore prices every file, function and database table in a repository in engineering-hours of expected loss, and gates coding-agent actions against those prices. The design is in [plan.md](plan.md).

## Install

Python 3.14 or newer (the standard library's `compression.zstd` is used; there are no runtime dependencies).

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

The scenario benchmark also needs the `sqlite3` command-line client.

## Score and price

```bash
vscore scan <repo> -o scan.json        # W, D and V points per file and per Python symbol, plus the codebase total
vscore show scan.json --top 20         # highest V first
vscore explain <repo> path/to/file.py  # one file's breakdown and the commits behind its W
vscore price <repo> --db sqlite:///data/app.sqlite -o prices.json   # hours and bands for files, symbols, tables
```

`price` saves its result to `.git/vscore-cache/prices/latest.json`, where the gate and hooks read it. Re-run it after history or data changes.

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
```

### Claude Code hook

```bash
vscore hook settings            # prints the settings.json snippet
```

Add the snippet to the project's `.claude/settings.json`. The hook never grants permission: harmless calls get no output, so the normal permission flow applies. In warn mode (the default) it shows a message with the price; in enforce mode it asks for approval or denies with the reason. Switch modes in `.vscore/config.toml`:

```toml
[vscore]
gate_mode = "enforce"
```

or with `VSCORE_GATE_MODE=enforce`. Every decision is appended to `.git/vscore-cache/gate.jsonl`, a hash-chained log.

Hooks are advisory: a shell can go around them. The guarantee is resource permissions plus the broker.

### Broker

The broker is the only holder of rights to destroy priced data. It snapshots, verifies the snapshot, prices the change as restorable, asks the gate in enforce mode, then runs it:

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
