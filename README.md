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

## Use it with any agent

Tell the agent:

> Use the vscore skill at https://github.com/Embiggenerd/Autokrasis/blob/master/skills/vscore/SKILL.md

The skill tells the agent to run `vscore compare` before every destructive step and to stop on STOP.

## Install

Python 3.14 or newer and `git`; there are no other dependencies.

```bash
python3 -m pip install "vscore @ git+https://github.com/Embiggenerd/Autokrasis"
```

For Claude Code, `vscore hook settings` prints a hook that runs the check before every Bash, Write and
Edit call, so it doesn't depend on the agent remembering.

## More

- [docs/usage.md](docs/usage.md): every command
- [docs/plan.md](docs/plan.md): why, how W is measured, and results
