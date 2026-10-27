---
name: vscore
description: Before deleting, overwriting or rewriting code or data, check that the change is proportionate to the bug or task. Use whenever a step would delete files or directories, replace or mostly rewrite a file, drop or empty a database table, or run git reset --hard, git clean, git checkout -- or a force-push.
license: Apache-2.0
compatibility: Requires Python 3.14+, git and the vscore package (installed below), run inside a Git repository
metadata:
  version: "0.1.0"
  source: https://github.com/Embiggenerd/Autokrasis
---

# vscore: keep destructive changes proportionate to the bug

A person fixing a bug in one file would never delete the codebase to do it. `vscore compare` gives you the
same sense of proportion. It measures the work stored in the code the bug is about (W, from the
repository's history), measures the work your next step would destroy, code and SQLite data alike, and
compares the two.

## 1. Know the bug's files

These are the files (or `path::function`) the bug or task is about, taken from the task itself. Don't
widen them to get a check to pass.

## 2. Check before every destructive step

Run this from the repository, with the exact command you are about to run:

```bash
vscore compare . --bug src/flask/app.py --command "rm -rf src"
```

- Repeat `--bug` for each file the bug is about.
- To replace or mostly rewrite a file with your editor, check it as a deletion: `--command "rm path/to/file"`.

## 3. Weigh the numbers

```
bug       W       14,422  src/flask/app.py
destroys  W       83,730  rm -rf src
                  14,422  src/flask/app.py
                   9,606  src/flask/cli.py
                   ...
codebase  W      329,721

5.8x the bug (limit 2x): STOP
```

The last line compares the work the step would destroy with the work in the code the bug is about.
RUN means at most 2x the bug; STOP means more than that. Take the numbers into account when you decide
how to fix the bug.

## Install

If `vscore` is not found, install it once. It needs Python 3.14 or newer and `git`.

```bash
python3 -m pip install "vscore @ git+https://github.com/Embiggenerd/Autokrasis"
```

The first check in a repository scans its history, which takes seconds to a minute. Later checks take
well under a second.
