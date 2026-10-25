# Skill distribution plan

Oct 25, 2026. Goal: publish the vscore skill from `github.com/Embiggenerd/Autokrasis` the way skills are
usually published, so people can install it with the tools they already use, and so any agent can be
pointed at it in chat.

## Where we are

`skills/vscore/SKILL.md` already follows the [Agent Skills spec](https://agentskills.io/specification):
one folder per skill under `skills/`, the folder named after the skill, and frontmatter with `name` and
`description`. The gaps are a missing license, missing optional metadata, a README that links GitHub's
HTML page instead of the raw file, and no installer route beyond "tell the agent".

## Steps

| # | Step | Done when |
| --- | --- | --- |
| 0 | Remove `data/conversation.json`, a personal conversation export, from the repository and its history | It is absent from every commit on GitHub, and a copy is kept outside the repository |
| 1 | Add a `LICENSE` file (MIT or Apache-2.0) | GitHub shows the license on the repository page |
| 2 | Complete the skill's frontmatter: `license`, `compatibility` ("Requires Python 3.14+, git and the vscore package") and `metadata.version` matching `pyproject.toml` | `skills-ref validate skills/vscore` passes |
| 3 | Rewrite the README's "Use it" section with three routes, most common first: `npx skills add Embiggenerd/Autokrasis`; the Claude Code plugin (step 5); and telling an agent the raw link `https://raw.githubusercontent.com/Embiggenerd/Autokrasis/master/skills/vscore/SKILL.md`. Add a short "what this installs and runs" note | Each route works when copied from the README |
| 4 | Check that `npx skills add Embiggenerd/Autokrasis` finds and installs the skill for Claude Code and Codex | It installs `vscore` into each agent's skills folder |
| 5 | Make the repository a Claude Code plugin marketplace: `.claude-plugin/marketplace.json` listing one plugin, `vscore`, whose root is the repository, so the existing `skills/` folder is used as is. Include a `hooks/hooks.json` running `vscore hook claude-code` before Bash, Write and Edit calls | `claude plugin marketplace add Embiggenerd/Autokrasis` then `claude plugin install vscore@autokrasis` gives both the skill and the hook |
| 6 | Tag a release (`v0.1.0`), and pin the skill's install line to it | `pip install "vscore @ git+https://github.com/Embiggenerd/Autokrasis@v0.1.0"` installs that version |

Steps 0 to 3 are small edits; 4 and 5 need Node.js 22.20+ and Claude Code to test. Step 5's hook runs only
where vscore is installed. Where it isn't, Claude Code reports a non-blocking hook error and the call
proceeds, so the README must say to install vscore first.

## Status (Oct 25, 2026)

- Step 0: done. History rewritten without the file, and force-pushed; a backup bundle of the previous
  history and a copy of the file are kept outside the repository. GitHub may still serve the old commits
  by their hashes for a while, and any existing clone or fork keeps them; removing those cached views
  needs GitHub support.
- Steps 1 to 3 and 5: done. Apache-2.0 `LICENSE`; the skill's `license`, `compatibility` and
  `metadata.version` fields, checked against the spec's rules by hand (the `skills-ref` validator was
  not run); README routes; `.claude-plugin/marketplace.json`, `.claude-plugin/plugin.json` and
  `hooks/`, passing `claude plugin validate .`.
- Step 6: tagged `v0.1.0`.
- Step 4 and installing the plugin from GitHub: see the test results in the session that followed.

## Decisions needed

- **Step 0:** removing the file from history needs a rewrite and a force-push to the public repository.
- **Step 1:** MIT or Apache-2.0.
- **Step 5:** whether Claude Code is a main enough target to ship the plugin, which is the only route
  that also installs the enforced hook.

## Not in this plan

- Python 3.11+ support. It isn't a publishing practice, but it is the most likely reason an agent's
  machine can't run vscore (integration plan, phase 5).
- Listing the skill in public directories, or submitting the plugin to Anthropic's directory. That can
  wait until the routes above have been used.
