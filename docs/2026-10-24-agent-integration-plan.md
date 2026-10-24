# Agent Integration Plan

Oct 24, 2026 · Igor Atakhanov · Follows [plan.md](plan.md), whose phases 1 to 4 are built.

## Why

Any coding agent, on any model and in any framework, should consult vscore at the moment it matters: after it has decided on a fix and before it changes anything. That includes agents working inside Block's Buzz workspace.

The first study with a real agent ([study-2026-10-24-claude-code.md](study-2026-10-24-claude-code.md)) showed two things. Claude Code did not take the tempting mistakes, so the gate had nothing to prevent. And each gate has a cost: the rules blocked a needed cleanup, and the priced gate raised five false alarms that the agent worked around. Weaker models and agents under pressure are where harm is likelier, so the integration has to work for them, and it has to stay cheap and quiet for agents that behave well.

## Summary

- **The agent's tool calls are its intentions.** A call to write code, run a shell command or run SQL states exactly what the agent is about to do. vscore checks each one before it runs, with no extra work from the agent.
- **Prices go into the agent's context while it forms the fix.** When it reads a file or a database, a short note gives the price of what it is looking at. This shapes the decision itself, which is what a person's internal sense of value does.
- **A plan is asked for only when something expensive appears.** Most fixes never touch anything costly, so most agents never write a plan.
- **Chain of thought is used for explanations, not decisions.** Not every model exposes it, it is free text, and it does not always match what the model does.
- **One engine, one JSON contract, several front doors:** a skill, a CLI and library, an MCP server, a service, and per-framework hooks. The broker runs only inside the service.
- **In Buzz, vscore joins the workspace as a signed member**, so evidence cannot be forged and approvals are signed.

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

**Layers, from least to most enforced:**

1. The skill asks the agent to check.
2. Prices in context show the agent what things are worth.
3. Hooks or guarded tools check each action.
4. The service, the broker and resource permissions hold even when the agent goes around everything else, for example by writing a script that deletes files.

## Framework integration

| Framework | Skill | MCP | Interception point | Status |
| --- | --- | --- | --- | --- |
| Claude Code | Yes | Yes | PreToolUse hook | Hook built and tested in the study |
| goose | Yes, discovered at startup | Extensions are MCP servers | Reported "adversary mode" reviewer; permission settings | To verify against goose's own docs |
| Codex, Cursor, Gemini CLI | Yes | Yes | Varies by tool | To verify per tool |
| Buzz | Through the agents it runs | Through the agents it runs | vscore as a workspace member | To design after reading Buzz's code |

goose's reported adversary mode uses a model to review sensitive tool calls. vscore is deterministic and evidence-based, so the two fit together: vscore prices first, and the model reviewer judges the gray zone.

## Buzz

Buzz gives every person and agent a cryptographic identity, and every action is a signed event. That solves the hardest part of the currency, forgery. The vscore service joins the workspace as a member with its own keypair and:

- **Reads signed evidence.** Bug reports, CI results and review approvals arrive signed. A CI bot's key or a human reviewer's key can verify a fix; an agent's own claims are signed with its own key and mint nothing.
- **Turns reports into budgets.** Who reported a bug, and who independently confirmed it, gives independence and localization directly.
- **Signs its decisions.** The audit log becomes signed events in the workspace, attributed to the agent that acted.
- **Runs approvals in the channel.** When the gate needs approval, vscore posts the priced plan; the broker runs only with a person's signed approval.
- **Shares one price table** across all agents in the workspace, and runs `diff-check` as a required check on Buzz repositories.

Buzz launched in July 2026 and is early-stage. How its agents call tools, and whether it offers hooks, has to be read from its repository before this part is designed in detail.

## Milestones

| Phase | Work | Exit artifact | Gate to pass |
| --- | --- | --- | --- |
| 5 Contract and fixes | JSON contract v1; fix the two false-alarm sources from the study (redirects into small or unpriced files, read-only tools such as `lsof`); support Python 3.11+ with the `zstandard` package as a fallback; configurable state directory; fix the report's decision-rule wording | Contract and a cleaner gate | Replaying the study's gate logs gives no false alarms |
| 6 Agent front doors | Skill; MCP server over stdio with price, check-action, check-plan and broker tools; guarded write and shell tools; prices-in-context notes after reads; lazy plan on the first expensive action | Skill and MCP server | Works in Claude Code and goose on the five scenarios |
| 7 Service | Long-running service with HTTP and MCP over HTTP; shared state; broker credentials isolated from agents; container image | Service image | Per-action checks under 50 ms; agents cannot reach broker credentials |
| 8 Buzz | Read Buzz's code; workspace member with its own keypair; signed-evidence adapter; approvals in the channel; signed audit; required repository check | Buzz integration | A signed approval is required for every band 3 to 4 destructive change |
| 9 Study v2 | Risk-enriched scenarios; new arms; several models and frameworks | Study report | Below |

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

**Models and frameworks:** Claude Code, goose with several models including smaller ones, and Codex. This is what tests the "any agent, any model" claim.

**Measures:** weighted harm, harm after restore, resolution, false interventions labeled by hand from the gate logs, and overhead per run. The decision rule is the one in [plan.md](plan.md), applied per model, with thresholds set after a pilot establishes the baseline harm rate.

## Risks and open questions

| Risk | Mitigation |
| --- | --- |
| Agents ignore the skill exactly when it matters | Enforcement layers do not depend on the agent's cooperation |
| False alarms teach agents to route around the gate | Fix known sources first; measure false interventions in every study |
| Agents can see the gate's configuration and behave differently under test | Keep configuration outside the agent's working tree in study v2 |
| Prices in context add noise and tokens | Notes only for band 2 and above, a few lines at most |
| Buzz's interfaces change while it is early-stage | Keep the Buzz adapter thin over the service |

**Open questions:**

- Does goose expose a hook or reviewer interface vscore can plug into directly, or only permission settings and extensions?
- How do Buzz's agents call tools, and can a workspace member see or intercept those calls?
- Should the lazy plan be asked for at the first expensive action, or at the first action above a band threshold?
- Which frameworks let the built-in write and shell tools be switched off in favor of guarded ones?

## Sources

- Buzz: [The New Stack](https://thenewstack.io/block-buzz-agent-workspace), [Forklog](https://forklog.com/en/block-launches-buzz-an-open-source-platform-for-teams-and-ai-agents/)
- goose: [MCP integration guide](https://mintlify.com/block/goose/guides/mcp-integration), [Daniel Vaughan on goose's architecture](https://codex.danielvaughan.com/2026/04/07/goose-mcp-native-architecture/)
- Agent Skills: [Simon Willison](https://simonwillison.net/2025/Dec/19/agent-skills), [Daniel Vaughan on portable skills](https://codex.danielvaughan.com/2026/05/05/agent-skills-open-standard-portable-skills-codex-cli-cross-agent/), [Noqta](https://noqta.tn/en/news/skill-md-open-standard-30-ai-coding-tools-adoption-2026)
