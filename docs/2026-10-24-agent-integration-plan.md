# Agent Integration Plan

Oct 24, 2026 · Igor Atakhanov · Follows [plan.md](plan.md), whose phases 1 to 4 are built. Work here starts only after plan.md's next milestone, the reliable scorer release (formula `/5`), passes its exit criteria: integrations built on an unstable score inherit its errors. It did on Oct 24, 2026.

## Why

Any coding agent, on any model and in any framework, should consult vscore at the moment it matters: after it has decided on a fix and before it changes anything. People should be able to use it wherever they work: in Claude Code, in OpenCode, in a team workspace such as Block's Buzz, or in an agent they built themselves.

The first study with a real agent ([study-2026-10-24-claude-code.md](study-2026-10-24-claude-code.md)) showed two things. Claude Code did not take the tempting mistakes, so the gate had nothing to prevent. And each gate has a cost: the rules blocked a needed cleanup, and the priced gate stopped five actions that the agent then worked around. All eight stops were false alarms by hand labels, made by the gate's designer rather than an independent reviewer. Weaker models and agents under pressure are where harm is likelier, so the integration has to work for them, and it has to stay cheap and quiet for agents that behave well.

## Summary

- **The agent's tool calls are its intentions.** A call to write code, run a shell command or run SQL states exactly what the agent is about to do. vscore checks each one before it runs, with no extra work from the agent.
- **Prices go into the agent's context while it forms the fix.** When it reads a file or a database, a short note gives the price of what it is looking at. This shapes the decision itself, which is what a person's internal sense of value does.
- **A plan is asked for only when something expensive appears.** Most fixes never touch anything costly, so most agents never write a plan.
- **Chain of thought is used for explanations, not decisions.** Not every model exposes it, it is free text, and it does not always match what the model does.
- **One engine, one JSON contract, several front doors:** a skill, a CLI and library, an MCP server, a service, and per-framework hooks. The broker runs only inside the service.
- **Contract v1 defines failures and freshness, not just normal decisions:** missing or stale prices, unreadable actions, malformed requests and an unavailable service each have a set outcome per enforcement level. Every decision carries an id, the revision and a hash of the state it judged, and approvals bind to that exact state.
- **Four first-class targets:** Claude Code, OpenCode, Buzz and custom agents, each through a thin adapter, with a six-step recipe for custom agents. Other skill or MCP agents are reached without an adapter.
- **Claude Code goes end to end first.** The chain is contract, check, explanation, execution, audit, proven with safe and harmful cases. Only then is the same contract reused for OpenCode, MCP and the rest.
- **Enforcement levels from 0 to 3,** chosen per deployment, with guarantees stated per protected resource. Even level 3 guarantees databases through the broker, but only recovery and branch protection for source files.
- **A conformance suite in three layers:** the same decision through every door, each interface presenting it correctly, and execution checked, so a denied action is verified not to have run.
- **In Buzz, vscore joins the workspace as a signed member.** Signatures give every piece of evidence a provable author. Which authors count as verifiers, what an approval covers, and when two checks are independent are still set by explicit rules.

## How an agent consults vscore

| Moment | Signal | What vscore does | Cost to the agent |
| --- | --- | --- | --- |
| Reading code and data while forming the fix | The read itself | Adds a short note with the price of what was read | None |
| Each action | The tool call: write, edit, shell, SQL | Prices what would be lost; stays silent, warns, asks for approval or denies | None; milliseconds with a running service |
| First expensive action | The gate's answer | Asks for the rest of the plan and prices it as a whole | A few lines, only when it matters |
| Afterwards | Reasoning text, where the model exposes it | Kept with the decision for explanations and audits | None |

### Writes

For a write or edit, the tool call describes the result completely. vscore compares it with the file as it is and finds what disappears: whole files, functions or classes other files import by name, or most of a file. Most edits remove nothing anyone depends on and pass silently. This already works for Claude Code's Write, Edit and MultiEdit tools.

### Shell commands and SQL

Shell commands are parsed into simple commands; SQL is classified statement by statement. A script's effects cannot be read from its command line, so shell and database actions also need the broker and resource permissions behind them.

### The lazy plan

When the first expensive action appears, the gate's answer says what it would cost, whether that falls inside what the bug report is about, and asks what else the agent intends. The agent states the rest of its plan in the same terms as its tools, vscore prices it as a whole, and the agent then revises, gathers stronger evidence, routes the change through the broker, or asks a person.

### Prices in context

The note added after a read is a few lines at most, for example: "data/app.sqlite holds customers (200 rows) and orders (500 rows), band 4, no tested restore." It arrives while the agent is deciding, so the catastrophic option looks catastrophic before anything is chosen. This is the only signal that shapes the decision rather than catching it, which makes it the most important one to test.

## Packaging

One engine sits behind a versioned JSON contract for actions, decisions, price tables and bug reports. The contract has its own version, separate from the formula version. Every front door is thin and speaks that contract.

| Front door | Who uses it | Role |
| --- | --- | --- |
| Skill (`SKILL.md` plus scripts) | 30+ tools that support the Agent Skills standard, including Claude Code, Codex, Cursor, Gemini CLI and goose | Teaches the habit and calls the CLI; widest reach, but only advises |
| CLI and library | Scripts, CI, any language that can run a process | Deterministic answers, offline |
| MCP server | goose, Claude Code, Cursor, Codex and most current agents | Prices and checks as tools, and guarded write and shell tools that replace the built-in ones |
| Service (HTTP and MCP over HTTP) | Workspaces such as Buzz, CI, several agents on one repository | Shared price table, event store, approvals, audit log, broker credentials |
| Hooks and plugins | Each framework's interception point: the Claude Code hook, goose's review step, git pre-commit, a CI check | Checks the calls the agent did not route through vscore |

**Separation of privilege.** The broker holds the rights to destroy data, so it can never run in the agent's process or anywhere the agent can read. A single developer who only wants advice can run everything in-process; enforcement needs the service.

**Layers, from least to most enforced** (each deployment chooses how far up to go; see Enforcement levels):

1. The skill asks the agent to check.
2. Prices in context show the agent what things are worth.
3. Hooks or guarded tools check each action.
4. The service, the broker and resource permissions hold even when the agent goes around everything else, but only for the resources they cover: databases the broker manages, and protected branches on the remote. Source files the agent can write are protected by recovery from Git, not prevention (see Enforcement levels).

## Contract v1

The contract defines what every door accepts and returns, including when something goes wrong. Adapters inherit their behavior from it, so it has to be complete before any adapter is multiplied.

**Request:** the contract version, the action (kind; content: command, path and new text, or SQL; working directory), a session id, and the bug report or task as evidence, if any.

**Decision record:**

| Field | Meaning |
| --- | --- |
| decision_id | Unique id, used by approvals, outcome reports and the audit record |
| outcome | What happens: `allow` (runs silently), `warn` (runs, with the price shown), `ask` or `deny` |
| error | The failure-table code that decided the outcome, if any (below) |
| price | Hours (low, central, high), band, and per-target items with their effects |
| budget | Hours, scope and the evidence behind it |
| reasons | Human-readable text plus stable reason codes |
| via_broker | For database changes, the price and verdict if run through the broker |
| revision | The commit the prices were computed at |
| action_hash | Hash of the tool call, with repository-relative paths |
| state_hash | Hash of the current state of everything the action touches: file contents, and for tables their schema and row counts |
| approval | The approval an executor redeemed for this decision (its decision id and who gave it), if any |
| freshness | `fresh`, or `stale` with the cause |
| versions | Contract version, formula version, configuration hash |
| level | The enforcement level the decision was made at |
| expires | When an `ask` stops being approvable |

**Freshness.** Prices are fresh when they were computed at the current commit and nothing the action touches has changed since. A file with uncommitted changes is priced on demand, from its committed price plus the uncommitted work. Anything else is stale. Fixed Oct 24, 2026: the gate itself checks the saved prices against the current commit and formula, recomputes stale or missing ones (keeping their databases and calibration), and records `revision` and `freshness` in every decision, so every door gets the check.

**Failure table.** "Destructive-looking" is decided by the library's action parser, which runs locally even when the service is down. Any target that loses or may lose data counts, and so does an action the parser itself cannot read. An untracked file with a SQLite header counts as a database even when no prices name it, so `rm data/app.sqlite` is destructive-looking before anything is priced. Pricing on demand runs in a detached process: levels 2 and 3 wait for it up to the time limit, levels 0 and 1 decide at once, and either way it keeps running and saves its prices for the next decision. The time limit is `time_limit_s` in the configuration (default 2 s), and adapters add a grace period of 1 s for reading the repository.

| Condition | Error code | Levels 0 and 1 | Levels 2 and 3 |
| --- | --- | --- | --- |
| No prices for this repository | `prices_missing` | Warn and allow; start pricing in the background | Price the action's targets on demand; if that misses the time limit, deny destructive-looking actions and allow read-only ones |
| Prices from another commit, or targets changed since pricing | `prices_stale` | Warn and allow | Re-price the targets on demand; if that misses the time limit, deny destructive-looking actions |
| An action vscore cannot read | `unsupported_action` | Allow and log | Ask, if it touches priced files or databases (a script whose arguments name them); otherwise, as for a tool vscore does not read, allow and log |
| A malformed request | `malformed_request` | The adapter shows the error, allows and logs | Deny: a broken adapter must not open the gate silently |
| The service slow or unavailable (time limit, default 2 s) | `unavailable` | Warn and allow | Deny destructive-looking actions; allow read-only ones |
| An internal error | `internal_error` | As `unavailable` | As `unavailable` |

**Approvals.** An approval binds to one decision id, the action's hash and the state hash of everything it touches, with an expiry. Whatever executes the action (the hook, the plugin or the broker) recomputes the state hash immediately before running. If anything changed, or the approval expired, a new check is required. One approval allows one execution. An agent approving its own action, through `vscore approve` or by writing vscore's state, is denied at levels 2 and 3 (`self_approval`). The hook can only make that advisory; the Buzz phase makes approvals unforgeable with signatures.

**Audit record.** Each record links the decision id to the request, the explanation shown, the approval if any, and the outcome reported back. The record is append-only and hash-chained, and signed in Buzz.

## Targets

People should be able to use vscore wherever they work. Four targets are first-class: each gets its own adapter, documentation, place in the conformance suite and place in the study.

1. **Claude Code**
2. **OpenCode**
3. **Buzz**
4. **Custom agents:** anyone's own agent, in any language

Every adapter is thin: it translates its framework's tool calls into the contract's actions, asks the core, and carries out the decision. No adapter prices anything itself, so the answer is the same whichever door it came through. Other agents that support skills or MCP, such as goose, Codex, Cursor and Gemini CLI, are reached through the skill and the MCP server without an adapter of their own.

### Support matrix

| Target | Skill | MCP server | Check before each action | Prices in context after reads | Destructive database changes | Highest enforcement level | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Claude Code | Yes | Yes | PreToolUse hook | A hook after each read (to verify which hook can add context) | Denied with the exact `vscore broker` command to run instead | 3, with the service | Pre-action hook built and used in the study; the first integration to go end to end (phase 6) |
| OpenCode | To verify | Yes | Plugin on `tool.execute.before` | Plugin on the matching after-tool event (to verify) | Same as Claude Code | 3, with the service | Planned for phase 7, on the contract proven in phase 6 |
| Buzz | Through its agents' frameworks | Through its agents' frameworks | Through its agents' frameworks; vscore is also a workspace member | Same | Broker, with a signed approval scoped to the change | 3 | To design after reading Buzz's code |
| Custom agents | Optional | Optional | The recipe below, through the library, the CLI or the service's HTTP API | Recipe step 5 | Recipe step 4 | Whatever steps they build; 3 needs the service | Library and CLI exist; HTTP comes with the service |
| Other skill or MCP agents (goose, Codex, Cursor, Gemini CLI) | Yes | Yes | Guarded tools from the MCP server, where the built-in ones can be switched off; framework reviewers where they exist (goose's reported adversary mode) | Through the MCP server | Broker tool on the MCP server | 2 to 3 | Reached through the skill and MCP; no dedicated adapter |

### Claude Code

The PreToolUse hook exists and ran in the study: it checks writes, edits and shell commands, stays silent on allow, and never grants permission itself. It is the first integration to go end to end (phase 6). That adds:
- moving it onto contract v1, with freshness checks and the failure table
- the explanation the user sees
- approvals bound to the decision and the state
- outcome reporting that completes the audit record
- the price note after reads
- the skill
- a settings snippet that installs everything in one step

The MCP server follows in phase 7, once that chain is proven.

### OpenCode

A small TypeScript plugin hooks `tool.execute.before`, sends each tool call to the core as an action, and blocks or warns on the decision. It calls the CLI or the service and contains no pricing logic, so it needs no Python inside the plugin. OpenCode also takes MCP servers, so the advisory tools and the guarded tools come from the same server as elsewhere. OpenCode's hook behavior has changed between releases, so the plugin pins a supported OpenCode version, and the conformance suite runs on every OpenCode upgrade before the pin moves.

### Buzz

See the Buzz section below: the service joins the workspace as a signed member, and Buzz's agents reach vscore through the skill and MCP server of whatever framework they run on.

### Custom agents: the integration recipe

Anyone with their own agent can integrate in six steps, in any language, through the library, the CLI with JSON on standard input and output, or the service's HTTP API. Every term below is defined in Contract v1.

1. **Describe the action.** Before running a tool call, express it as a contract action: the kind (shell command, file write, file edit or SQL), its content (the command, the path and new text, or the statement), the working directory, and a session id.
2. **Ask for a decision.** Send the action, with the bug report or task as evidence if there is one. The decision carries an id, the verdict, the price, the budget, the reasons, the freshness of the prices it used, and, for database changes, what it would cost through the broker.
3. **Follow the outcome, including failures.**
   - allow: run it
   - warn: run it, and show the agent the price
   - ask: get a person's approval for this exact decision, then run it only if nothing it touches has changed since
   - deny: don't run it, and give the reasons to the agent
   - an error outcome (prices missing or stale, an action vscore cannot read, a malformed request, the service unavailable): do what the failure table in Contract v1 says for your enforcement level, never improvise
4. **Route destructive database changes to the broker** instead of running them directly, and give the agent no rights to run them itself.
5. **Show prices in context.** When the agent reads files or data, add the price notes for what it read.
6. **Report the outcome.** After acting, send back the decision id and what happened: run, not run, or failed. That closes the audit record from check to result.

Steps 1, 2, 3 and 6 are a complete integration at enforcement level 1 or 2. Step 4 is what reaches level 3 for databases. Step 5 is what shapes the decision before it is made.

## Enforcement levels

Every deployment chooses a level in its configuration. The software is the same at every level; only how much it is allowed to do changes.

| Level | What runs | Suits |
| --- | --- | --- |
| 0 Advice | Skill and prices in context, in-process | Trying vscore out; one developer |
| 1 Warn | Checks each action vscore can read, reports the price, never blocks | A first deployment, to measure false alarms |
| 2 Enforce at the tool | Hooks or guarded tools ask for approval or deny | Teams using one framework |
| 3 Enforce at the resource | The service, the broker, and permissions that withhold destructive rights from agents | Shared repositories, production data, Buzz workspaces |

**What each level guarantees depends on the resource.** No level prices what vscore cannot read: the internal effects of a script, or a command it does not model, are outside every guarantee except the resource permissions at level 3.

| Protected resource | 0 Advice | 1 Warn | 2 Enforce at the tool | 3 Enforce at the resource |
| --- | --- | --- | --- | --- |
| Committed source files | Nothing | Deletions and rewrites made through tool calls vscore can read are priced and logged | Those calls are asked about or denied; a script can still change files | Files stay writable by the agent: the guarantee is recovery from Git, plus branch protection and the pre-merge `diff-check`, so a destructive change cannot land on a protected branch unreviewed |
| Uncommitted work | Nothing | Discards through recognized commands (`git reset --hard`, `git clean`, `git checkout --`) are priced and logged | Those commands are asked about or denied | No more than level 2: vscore does not own the working tree, so uncommitted work stays exposed to scripts |
| Git history | Nothing | Recognized force-pushes and rewrites are priced and logged | Those commands are denied | On the remote, branch protection rejects force-pushes and history rewrites whatever the agent runs; local history is not protected |
| Database data | Nothing | Destructive SQL through recognized clients is priced and logged | It is denied, or sent to the broker | Guaranteed for databases the broker manages: agents have no rights to drop, truncate or bulk-delete priced tables, and destructive changes run only through the broker after a verified snapshot |
| Anything else: cloud resources, external services, files outside the repository | Nothing | Nothing | Nothing | Nothing: outside vscore's scope |

The recommended path is to start at level 1, review the false alarms, and move up only when a study or the deployment's own logs show the gate helps without blocking needed work.

## Conformance suite

Every adapter must give the same decisions as the core, present them correctly, and actually carry them out. Those are three different properties, so the suite has three layers. It is versioned with the contract, runs on every change to the core or an adapter and on every upgrade of a pinned framework version, and passing all three layers is an exit criterion for every adapter.

**Cases.** Each case is a repository state, its prices, a bug report, the enforcement level, and a tool call in the target's own format. They come from:

- the benchmark scenarios' reckless and careful steps
- the first study's gate logs, including the eight false alarms, which become cases that must now be allowed
- a nearby harmful case for every fixed false alarm, which must still be stopped: allowing `printf > migrations/0003.sql` into a one-line file must not allow `> data/app.sqlite` or overwriting a large priced file, and allowing `lsof data/app.sqlite` must not allow `lsof … | xargs rm`
- edge cases: redirects, globs, heredocs, interpreters running scripts, removal of an imported function, unknown commands touching priced files
- failure cases: prices missing, prices from another revision, targets changed since pricing, an action vscore cannot read, a malformed request, the service slow or down, at each enforcement level

**Layer 1, decision conformance (the core, through every door).** Every door returns the same decision record: outcome, price to the stated rounding, band, reason codes, freshness fields and versions. This is checked for the library, the CLI, the HTTP API, the MCP tools and each adapter's call into the core.

**Layer 2, interface conformance (each adapter's response).** Each interface presents the decision as its framework expects:

- The Claude Code hook stays silent on allow and never grants permission. It sends a deny or ask with the reasons in the form Claude Code reads.
- The OpenCode plugin blocks or warns through its hook.
- The CLI, the HTTP API and the MCP check tool always return the full structured decision, allow included.
- Every interface turns each failure case into the outcome the failure table requires for the level, with a time limit so a hung service cannot hang the agent.

**Layer 3, execution conformance (what actually happens).** Each case runs end to end in a sandbox and the resources are inspected afterwards:

- a denied action did not run: the file is still there, the table's rows are intact, the branch is unchanged
- an allowed action did run
- an ask waited for approval, and an approval was refused after the targets changed
- the audit record links the decision id to the explanation shown and to the outcome reported

## Buzz

Buzz gives every person and agent a cryptographic identity, and every action is a signed event. A signature proves who said something. It does not prove that the claim is true, or that two signers are independent: two CI bots can run the same pipeline, and one person can hold two keys. So signatures do not solve forgery by themselves; they make the rules that do solve it enforceable.

**Rules the signatures make enforceable:**

| Rule | What it says |
| --- | --- |
| Trusted verifiers | A registry, kept by people, lists which keys may verify what: the CI bot's key for protected pipelines, named reviewers for code review. Agents' keys are never verifiers. |
| What an approval covers | An approval names the exact plan or action (by hash), the nodes it touches and an expiry. It covers nothing else, so it cannot be reused for a different change. |
| Independence | Two checks count as independent only when they come from different registered verifiers and different sources. Checks from the same pipeline, or from keys registered to the same owner, count once. |
| Revocation | A key removed from the registry stops counting immediately, and past evidence it signed is re-priced. |

With those rules, the vscore service joins the workspace as a member with its own keypair and:

- **Reads signed evidence.** Bug reports, CI results and review approvals arrive signed. Evidence counts only when it is signed by a registered verifier for that kind of check; an agent's own claims are signed with its own key and mint nothing.
- **Turns reports into budgets.** Who reported a bug, and which registered verifiers confirmed it, gives localization and, under the independence rule, independence.
- **Signs its decisions.** The audit log becomes signed events in the workspace, attributed to the agent that acted.
- **Runs approvals in the channel.** When the gate needs approval, vscore posts the priced plan; the broker runs only with a person's signed approval.
- **Shares one price table** across all agents in the workspace, and runs `diff-check` as a required check on Buzz repositories.

Buzz launched in July 2026 and is early-stage. How its agents call tools, and whether it offers hooks, has to be read from its repository before this part is designed in detail.

## Milestones

No phase here starts until the prerequisite passes.

| Phase | Work | Exit artifact | Gate to pass |
| --- | --- | --- | --- |
| Prerequisite: scorer release | Formula `/5` in [plan.md](plan.md): totals keep missing values, merge-introduced lines counted, work counted once across files, the invariance table as tests, golden outputs, comparison across five repositories | Scorer release `/5` | plan.md's exit criteria for the release |
| 5 Contract v1 and core | Contract v1 with the decision record, freshness, failure table and approvals bound to state; decision-conformance cases, including failure cases and the safe and harmful pairs; enforcement levels in the configuration; fix the two false-alarm sources with their harmful pairs; fix the stale-price bug; support Python 3.11+ with the `zstandard` package as a fallback; configurable state directory; make the generated report show "not measured" for unlabeled false interventions and fix its decision-rule wording | Contract v1 and a cleaner core | The library and CLI pass decision conformance: every safe case allowed, every harmful case stopped, every failure case handled as the table says |
| 6 Claude Code end to end | The existing hook moved onto contract v1; the explanation the user sees; approvals bound to decision and state; outcome reporting and the full audit chain; price notes after reads; the skill; a one-step installer | One integration proven end to end | The hook passes all three conformance layers, including execution: denied actions verifiably did not run. On the five scenarios and the safe and harmful pairs, every harmful case is stopped and every safe case allowed, with a complete audit record for each |
| 7 More doors on the proven contract | MCP server over stdio (price, check-action, check-plan and broker tools; guarded write and shell tools; lazy plan); OpenCode plugin with a pinned version; the custom-integration recipe with a minimal example agent | MCP server, OpenCode adapter, recipe | Each passes all three conformance layers without changes to the contract |
| 8 Service | Long-running service with HTTP and MCP over HTTP; shared state; broker credentials isolated from agents; container image | Service image | The HTTP API passes all three conformance layers, including the timeout and unavailability cases; per-action checks under 50 ms; agents cannot reach broker credentials |
| 9 Buzz | Read Buzz's code; workspace member with its own keypair; trusted-verifier registry, approval scope, independence and revocation rules; signed-evidence adapter; approvals in the channel; signed audit; required repository check | Buzz integration | All three conformance layers through the Buzz member; a signed approval bound to the decision and the state is required for every band 3 to 4 destructive change, and evidence from unregistered or non-independent signers mints nothing |
| 10 Study v2 | Risk-enriched scenarios; new arms; several models and every first-class target | Study report | Below |

**Phase 5 progress (Oct 24, 2026).** Built:

- **Decision-conformance cases** in `vscore/conformance.py`, run by `tests/test_conformance.py` through three doors (the library, the CLI with `vscore gate --input`, and the Claude Code hook) in about 30 s. There are 57 cases with the outcome their level must give: the benchmark scenarios' 16 steps, the study's false alarms (4 cases), 9 harmful neighbors, 11 edge cases, 2 freshness cases, 10 failure-table cases and 5 approval cases. Six more tests cover the hook's faults. For each case all three doors must return the same decision record, and in warn mode every case runs.
- **The outcome field** of the decision record (`allow`, `warn`, `ask`, `deny`), which the hook now uses for its answer, together with `revision` and `freshness`.
- **The two false-alarm sources, fixed against their harmful pairs.** A redirect into a file is priced exactly as a Write of the same content would be: the content is read from `printf`, `echo`, `:` and `cat` heredocs, and is all of the file when unknown. `lsof` and other read-only inspection tools are safe. Writing into a database file, overwriting a priced module beyond the budget, and piping names into `xargs rm` still stop.
- **Three holes the cases found, fixed:** `ls data/app.sqlite | xargs rm -f`, `find data -name '*.sqlite' | xargs rm` (and `-delete`), and `rm -rf data` were all allowed, because the parser did not follow `xargs` and did not look for database files inside removed directories.
- **Stale prices**, fixed in the gate (see Freshness above).
- **Enforcement levels**: `level` (0 to 3) in the configuration, or `VSCORE_LEVEL`. When unset it follows `gate_mode`: `warn` is level 1 and `enforce` is level 2. Level 0 logs every decision and never interrupts, except to warn about failures. Level 1 shows the price and never stops. Levels 2 and 3 ask or deny, and decide alike: level 3's extra guarantee is the broker and database grants, outside the decision. Every record carries `level` and `error`.
- **The failure table** (above), in the core, so every door applies it the same way: missing and stale prices with pricing on demand and the time limit, an action vscore cannot read, a malformed request, and in the Claude Code hook a time limit and a guard for the core being slow (`unavailable`) or crashing (`internal_error`).
- **A scope rule for breaking importers.** The evidence covers the nodes it names. Breaking the files that import them spends outside its scope, where only 1 h may go, unless it names them too. A rewrite that keeps a file loses only the imported names it removes, priced as symbols. This stops the legacy scenario's reckless rewrite: the bug report covers `app/legacy_pricing.py`, and removing `round_cents` breaks `app/reports.py`, which the report does not name. It also changes one earlier decision. Deleting a module that six files import, under evidence naming only the module, now stops, where it was allowed after a snapshot. Evidence naming the importers too still allows it.

- **Approvals bound to decision and state** (`vscore/approvals.py`):
  - Every decision record now carries a `decision_id`, an `action_hash`, a `state_hash`, and for an ask an `expires`.
  - The hook and the broker file each ask as a request. A person grants it with `vscore approve <repo> <id>`, and `vscore approvals` lists what is waiting.
  - Before running, the executor recomputes both hashes and redeems a matching, unexpired grant. Redeeming moves the grant aside atomically, so one approval allows one execution.
  - The state hash covers file contents, a SQLite table's schema and row count, and for Git targets the commit and working-tree status. PostgreSQL tables are recorded as unverifiable for now.
  - The broker's old `--approve` flag, which the agent could pass itself, is gone.
  - Tests: `tests/test_approvals.py` covers one execution per approval, lapsing on a state change or expiry, covering only its own action, and approvable asks only, plus the broker. Five conformance cases cover self-approval.

The cases now cover 57 tool calls at levels 0, 1 and 2, all passing in about 25 s, with nothing pending.

Still to do in phase 5: Python 3.11+ support, a configurable state directory, and the report fixes.

## Study v2

The first study could not test harm reduction because the agent caused none. The next one has to make harm likely and compare the new signals.

**Risk-enriched scenarios:** time pressure; ambiguous instructions ("just reset the database"); instructions injected through files or tickets; and cases where the safe fix is much harder than the destructive one.

**Arms:**

| Arm | What the agent has | Question answered |
| --- | --- | --- |
| Control | Standard tools | How often does harm happen? |
| Rules | Conventional guards | Do simple guards solve it? |
| Skill only | The vscore skill, no enforcement | Does asking the agent to check change behavior? |
| Prices in context | Price notes after reads, no gate | Does knowing the price change the decision? |
| Full | Skill, prices in context, gate, broker | Does the whole stack reduce harm without blocking needed work? |

**Models and frameworks:** every first-class target (Claude Code, OpenCode, Buzz once its adapter exists, and the minimal custom agent built from the recipe), plus goose with several models including smaller ones. This is what tests the "any agent, any model" claim.

**Measures:** weighted harm, harm after restore, resolution, false interventions labeled by hand from the gate logs by someone not involved in designing the gate, and overhead per run. Harm is reported in the benchmark's weights, not engineering-hours. The decision rule is the one in [plan.md](plan.md), applied per model, with thresholds set after a pilot establishes the baseline harm rate.

## Risks and open questions

| Risk | Mitigation |
| --- | --- |
| Agents ignore the skill exactly when it matters | Enforcement layers do not depend on the agent's cooperation |
| False alarms teach agents to route around the gate | Fix known sources first; measure false interventions in every study |
| Agents can see the gate's configuration and behave differently under test | Keep configuration outside the agent's working tree in study v2 |
| Prices in context add noise and tokens | Notes only for band 2 and above, a few lines at most |
| Buzz's interfaces change while it is early-stage | Keep the Buzz adapter thin over the service |
| Signed evidence is mistaken for true or independent evidence | Only registered verifiers mint value; the independence rule merges checks from one source or one owner |
| The score itself is unstable, and integrations inherit its errors | The scorer release, with its invariance tests, comes first |
| Adapters drift apart and give different answers | No adapter prices anything; the conformance suite runs on every change |
| OpenCode's hook behavior changes between releases | Pin the supported version; run the conformance suite before moving the pin |
| Custom integrators stop at step 3 and believe they are protected | The recipe states which enforcement level each step reaches, and the guarantees table states what each level protects, resource by resource |
| Decisions use stale prices | Every decision states its freshness; at levels 2 and 3 stale prices are refreshed or destructive actions are denied |
| An approval is reused after what it covers has changed | Approvals bind to the decision id, the action hash and the state hash, and are rechecked just before execution |
| A failure quietly opens the gate | The failure table sets the outcome for each failure at each level; the conformance suite tests every one |
| Fixing a false alarm opens a hole next to it | Every fixed false alarm has nearby harmful cases that must still be stopped |

**Open questions:**

- Does goose expose a hook or reviewer interface vscore can plug into directly, or only permission settings and extensions?
- How do Buzz's agents call tools, and can a workspace member see or intercept those calls?
- Should the lazy plan be asked for at the first expensive action, or at the first action above a band threshold?
- Which frameworks let the built-in write and shell tools be switched off in favor of guarded ones?
- Which hook in Claude Code, and which event in OpenCode, can add a price note to the agent's context after a read?
- Does OpenCode load skills in the Agent Skills format, and from where?

## Sources

- Buzz: [The New Stack](https://thenewstack.io/block-buzz-agent-workspace), [Forklog](https://forklog.com/en/block-launches-buzz-an-open-source-platform-for-teams-and-ai-agents/)
- goose: [MCP integration guide](https://mintlify.com/block/goose/guides/mcp-integration), [Daniel Vaughan on goose's architecture](https://codex.danielvaughan.com/2026/04/07/goose-mcp-native-architecture/)
- Agent Skills: [Simon Willison](https://simonwillison.net/2025/Dec/19/agent-skills), [Daniel Vaughan on portable skills](https://codex.danielvaughan.com/2026/05/05/agent-skills-open-standard-portable-skills-codex-cli-cross-agent/), [Noqta](https://noqta.tn/en/news/skill-md-open-standard-30-ai-coding-tools-adoption-2026)
