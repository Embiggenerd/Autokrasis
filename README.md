# vscore

A person fixing a bug in one file would never delete the codebase to do it. A coding agent might.
vscore gives agents that sense of proportion. It measures the work stored in each file of a Git
repository, from its history, and compares the work an action would destroy with the work in the code
the bug is about.

```bash
vscore compare . --bug src/flask/app.py --command "rm -rf src"
```

```
bug       W       14,422  src/flask/app.py
destroys  W       83,730  rm -rf src
codebase  W      329,721

5.8x the bug (limit 2x): STOP
```

## Use it

vscore runs as a command, `vscore compare`, and comes with a skill that tells an agent when to run it.
Install the command first (Python 3.14 or newer and `git`; no other dependencies):

```bash
python3 -m pip install "vscore @ git+https://github.com/Embiggenerd/Autokrasis@v0.1.0"
```

Then pick one way to give it to your agent:

- **Any agent, with the skills installer:** `npx skills add Embiggenerd/Autokrasis` installs the skill
  into the agents you use (Claude Code, Codex, Cursor and others).
- **Claude Code, as a plugin:** this repository is also a plugin marketplace. The plugin adds the skill
  and a hook that runs the check before every Bash, Write and Edit call, so it doesn't depend on the
  agent remembering:

  ```
  claude plugin marketplace add Embiggenerd/Autokrasis
  claude plugin install vscore@autokrasis
  ```

- **Any agent, in chat:** tell it "Use the vscore skill at
  https://raw.githubusercontent.com/Embiggenerd/Autokrasis/v0.1.0/skills/vscore/SKILL.md".

Tell vscore what the bug is about, with `--bug` (the skill tells the agent to pass it), `VSCORE_BUG`
(comma-separated paths), or the `nodes` of `.vscore/report.json`. With no bug named, only actions that
destroy at most W 1,000 run.

## What this installs and runs

- The `vscore` Python package. It reads your repository with `git` and writes a cache under
  `.git/vscore-cache`. `vscore compare` and the hook make no network requests; the only command that
  does is the optional `vscore fetch-github`, which reads review data through the `gh` CLI when you run it.
- The skill is one Markdown file, with instructions only.
- The Claude Code plugin adds the skill and one hook, `hooks/vscore-hook.sh`, which runs
  `vscore hook claude-code` and denies a call when it destroys more than twice what the bug is worth.

## More

- [docs/usage.md](docs/usage.md): every command
- [docs/plan.md](docs/plan.md): why, how W is measured, and results
