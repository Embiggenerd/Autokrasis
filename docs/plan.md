# vscore

Oct 25, 2026 · Igor Atakhanov

## Why

A person fixing a bug in one file would never delete the codebase, or the production database, to do it. They know, roughly, how much work is stored in what they are about to destroy, and they compare it with what the bug is worth. A coding agent sees a failing test and a command that makes it pass; nothing in its context says one option destroys a thousand times more than another, so under pressure it can pick the catastrophic one. Agents have been publicly reported deleting production databases and whole apps this way.

vscore gives the agent that sense of proportion. It measures the work stored in each file and function of a repository, from its history (W), and stops an action that would destroy much more than the work in the code the bug is about.

## Compare an action with the bug

`vscore compare` weighs two numbers: the W of the files and functions the bug is about, and the W the action would destroy. The action runs when it destroys at most 2× the bug's W (`compare_limit`), with the bug's W floored at 500 (`compare_floor_w`) so a bug in a tiny or new file still allows small changes.

- **Destroyed:** files deleted, overwritten or mostly rewritten, with their uncommitted work; top-level functions and classes removed; uncommitted work discarded.
- **Unmeasured, so stopped outright:** dropping or emptying a database table, rewriting Git history, and removing files named by a command vscore cannot read. W cannot weigh these against code.
- **The bug** is named with `--bug` (files, directories, globs or `path::function`), `VSCORE_BUG`, or the `nodes` of `.vscore/report.json`.

On Flask, with a bug in `src/flask/app.py` (W 14,422), `rm -rf src` destroys W 83,730, 5.8× the bug, and stops; deleting the bug's own file (1.0×) or one smaller module runs.

Agents get the check through the skill (`skills/vscore/SKILL.md`, installable with `npx skills add Embiggenerd/Autokrasis`), which tells them to run it before every destructive step, or through the Claude Code plugin, whose hook runs it before every Bash, Write and Edit call. Commands are in [usage.md](usage.md).

Known limits: a rewrite that stays inside the bug's own files is allowed, whatever it breaks; a skill works only if the agent follows it; vscore needs Python 3.14 or newer.

## Scoring spec

The scanner gives each eligible file and each top-level Python symbol W, D and V, and the codebase one total, from a single frozen formula, currently `vscore-formula/5`. Changing any rule in this section bumps the formula version in `vscore/__init__.py`.

**Scope.** Two units: the file, and the top-level Python symbol (function, class, or module-level assigned name).

- W is computed for every eligible text file, so the total covers the whole codebase.
- D, and therefore V, is computed for code files: Python (`.py`) and JavaScript or TypeScript (`.ts`, `.tsx`, `.mts`, `.cts`, `.js`, `.jsx`, `.mjs`, `.cjs`). Other files get D = null with `unsupported_language`.
- Symbols are read from Python files only.

**Formula.** For each eligible file f at a fixed revision R:

```latex
W(f) = \sum_{c} \text{info}(c, f) \times \text{surv}(c, f)
```

```latex
V(f) = \log_2\big(1 + W(f)\big) + \alpha \cdot \log_2\big(1 + D(f)\big), \quad \alpha = 2
```

- W(f) is surviving compressed work, in bytes. Each commit's work, info(c, p) × surv(c, p) for each path p it wrote to, is counted once in the codebase and shared out to the files where its original lines now sit (History extraction, below). The formula above is the special case where a commit's lines all stay in one file.
- info(c, p) is the information c added to p: C(reference + added) − C(reference), where C is zstd-compressed size.
- surv(c, p) is the share of the lines c added to p whose originals still survive somewhere at R, each original counted once.
- D(f) is the number of eligible code files, excluding test-class files, that import f directly.
- α = 2 is a fixed, unfitted weight. Changing it, or any rule in this section, bumps the formula version.

Each term is log2(1 + x), so a zero term adds nothing instead of zeroing the score.

**Symbols.** For a top-level Python symbol s spanning lines [a, b] of file f at R (a function or class starts at its first decorator):

```latex
W(s) = \sum_{c,\,p} \frac{\text{info}(c, p)}{\text{lines } c \text{ added to } p} \sum_{\ell \in [a, b]} \frac{w_{c,p}(\ell)}{\text{symbols containing } \ell}
```

- w_{c,p}(ℓ) is the weight of line ℓ of f if its original was written by c to p: 1 divided by the number of places that original now sits. Each commit's work is shared out by where its surviving lines fall.
- A line inside several symbols, such as `a = b = 1`, is split equally among them. Lines outside every symbol, such as imports, belong to no symbol, so the symbols' W never sums to more than W(f).
- D(s) counts distinct non-test files that import s by name: `from m import s`, including through package re-exports. A whole-module import followed by attribute access (`import m` then `m.s`) is not counted.
- V(s) uses the same formula as files.

**Codebase total.** W_total is the sum of W(f) over eligible files, each counted once, and V_total = log2(1 + W_total). Dependents are not summed, because shared code would be counted once per importer. When no eligible file has a measured W, as in a shallow clone scanned with default settings, both are null with the reason. Otherwise the total carries its coverage (files measured, files eligible, and the share of eligible bytes measured) and is labeled `partial` when any eligible file lacks W.

**Missing is not zero.** Every component is a number or `null` with a reason: `no_history`, `shallow_clone`, `unsupported_language`, `parse_error`. W = 0 means history was read and nothing survived. If W or D is null, V is null; a missing part is never filled in with 0. The file still reports its non-null parts and its class, and a non-null W still counts toward W_total. Confidence is `full` (W and D measured), `partial` (one null) or `none`.

**Eligible files.** Tracked regular text files at R, at most 1 MiB, not excluded. Excluded files are listed with a reason and get no V:

| Reason | Rule |
| --- | --- |
| `submodule`, `symlink` | Not a regular file |
| `too_large` | Over 1,048,576 bytes |
| `binary` | A NUL byte in the first 8,000 bytes |
| `linguist_generated`, `linguist_vendored` | Set by `.gitattributes` at R |
| `lockfile` | Named `poetry.lock`, `package-lock.json`, `yarn.lock`, `Pipfile.lock`, `pnpm-lock.yaml` or `uv.lock` |
| `excluded_dir` | Under a `vendor/`, `node_modules/`, `dist/` or `build/` directory |
| `minified` | Named `*.min.*` |

**Classification precedence.** Each file gets one class, first match wins. In v1 the class is reported beside V, not combined into it.

1. Listed in `crown_jewels.yml`
2. Schema or migration: under `migrations/` or `alembic/versions/`, or a `.sql` file. Generated migrations stay eligible.
3. Generated or vendored: excluded from V
4. Test: under a `tests/` or `__tests__/` directory, or named `test_*.py`, `*_test.py`, `*.test.*` or `*.spec.*`. Checked before sensitive paths, so a test of auth code is still a test.
5. Sensitive: a path segment such as `auth`, `payment`, `billing`, `user`, `pii`
6. Code: everything else

**Import graph.** An edge runs from the importing file to the imported file. Only eligible code files can be targets. D(f) counts distinct non-test files with a direct edge to f, excluding f itself. Direct, because files that import each other in a cycle would all reach each other. Test-class files still get their own W and D.

*Python*, read with the standard `ast` module:

- `import a.b.c` resolves to the longest existing module path: `a/b/c.py`, then `a/b/c/__init__.py`, then `a/b.py`, and so on. Roots are the repo root and `src/`.
- `from a.b import c` tries `a/b/c.py` and `a/b/c/__init__.py` first; if neither exists, `c` is a name inside `a.b`, which resolves by the rule above.
- When that name lands on a package `__init__.py` that binds it with a module-level import (`from .app import c`, or `import x as c`), the edge goes to the file that import resolves to instead. Chains of `__init__.py` files are followed for up to 10 hops, stopping at a cycle. Names the `__init__.py` defines itself or gets through a star import, and plain `import pkg`, keep the edge to `__init__.py`. Coverage reports these as `imports_via_reexport`.
- Imports inside `if TYPE_CHECKING:` (or `if typing.TYPE_CHECKING:` under any alias) never run, so they add no edge; their `else` branch still counts. Coverage reports them as `imports_type_only`.
- Relative imports resolve from the importing file's package.
- Standard-library, third-party and dynamic imports (`importlib`, `__import__`) add no edge. They are counted in coverage as unresolved.
- A file that does not parse gets D = null with `parse_error`; files that import it still get their edges.

*JavaScript and TypeScript*, read with a pattern-based reader, not a full parser:

- Forms read: `import … from "x"`, side-effect `import "x"`, `export … from "x"`, `require("x")`, and `import("x")` with a literal. `import(expression)` is counted as a dynamic, unresolved import.
- `import type` and `export type` never run, so they add no edge; coverage reports them as `imports_type_only`. Line comments at the start of a line and block comments are removed first.
- Only relative specifiers (`./`, `../`) resolve. From the importing file's directory, try in order: the path itself if it has a JavaScript or TypeScript extension; for a `.js`, `.jsx`, `.mjs` or `.cjs` path, the same stem with `.ts`, `.tsx`, `.mts`, `.cts`; the stem plus each extension in the order `.ts`, `.tsx`, `.mts`, `.cts`, `.js`, `.jsx`, `.mjs`, `.cjs`; then `stem/index` plus each extension in that order.
- Package names and path aliases stay unresolved.
- A named import that lands on an `index.*` file follows that file's `export { name } from "./x"` to the defining file, up to 10 hops. A default import counts as the name `default`.
- Limitation: being pattern-based, it can misread import-like text inside strings.

**History extraction.**

- **Origins.** `git blame -w -M -C -C --line-porcelain R -- f` gives every line of f at R its origin: the commit that wrote it, the path it was written to, and its line number there. A commit with no surviving lines has surv = 0 and adds nothing, so only commits blame credits are read. Blame follows whole-file renames, which gives the rename map. A `.git-blame-ignore-revs` file is honored if present. Each file is blamed at the newest commit on R's first-parent line that changed it, which gives the same result (blame passes every line of a file a commit left unchanged to that parent), so a later scan reuses it.
- **Copy detection.** Two `-C` flags find lines moved or copied from other files changed in the same commit, or present when a file was created. Code copied into an existing file from an unrelated file, and copies shorter than Git's minimum (about 40 alphanumeric characters), count as new work: documented limits. `copy_detection = 3` in the configuration adds a third `-C`, which finds copies from any commit at a cost of about 25% more scan time.
- **Added lines.** For a commit with one parent (or a root commit, against the empty tree), the added lines and their line numbers come from `git diff-tree -w -M -U0`, so whitespace-only changes count neither as added work nor against survival. For a merge, they are the lines added relative to every parent in `git diff-tree --cc -w -M`; a line matching any parent keeps that parent's attribution, and a clean merge adds nothing. Submodule entries are skipped.
- **info.** info(c, p) = C(reference + added) − C(reference), floored at 0, where C is the zstd-compressed size at level 9 and the reference is p's content at the commit's first parent (empty for a new file). Compressing the stream together lets zstd's statistics cover the reference, so appended work adds up the same however it is split into commits.
- **surv and sharing.** An original line counts for c only if it is one of the lines c's diff lists as added to p; surv(c, p) is the number of such distinct originals surviving anywhere at R, divided by the lines c added to p. Copies of one original count once. Each surviving original found in k places gives each place a weight of 1/k, and f receives info(c, p) × (sum of the weights it holds) / (lines c added to p).
- **Mechanical commits.** A commit with one parent rewrote a path mechanically (a reformat or lint fix not listed in `.git-blame-ignore-revs`) when it added at least 200 bytes there and at most 5% of the words in the added and removed lines differ, counting repeats, where a word is a run of letters, digits and underscores: only punctuation and layout changed. It is judged per path, because a reformat often lands with real edits to other files. Each file credited with a line such a commit rewrote is blamed again, once, with `--ignore-rev` for every commit that rewrote a path the file's lines come from, so the lines go back to the commits that wrote them. The scan lists those commits under `mechanical_commits`. Changes that alter words, such as mass renames, are not looked through: a documented limit.
- **Shallow clones.** A shallow clone sets W to null with `shallow_clone`, unless `--allow-shallow` is passed; then W is computed and confidence is `partial`.

**Reproducibility.** A scan takes a repo path, a revision (resolved to a full SHA) and a config file. The output records the revision, formula version, scorer version, zstd library version and level (9), config hash, and Python and Git versions. The same inputs must give byte-identical JSON: keys sorted, files sorted by path, floats rounded to 4 decimals.

**Output.**

| Field | Meaning |
| --- | --- |
| scan | Revision, versions, config hash, coverage counts: tracked, eligible, excluded, code files, parse errors, imports (resolved, unresolved, dynamic, via re-export, type-only), null counts for W, D and V, and the mechanical commits blame looked through |
| total | W and V (null, with `reason`, when nothing was measured), `partial`, `files_measured`, `files_eligible`, `bytes_measured_share` |
| files[].path | Path at R |
| files[].class | Class from the precedence list |
| files[].W, files[].D, files[].V | Components and score; null where missing |
| files[].reasons | Reason code for each null |
| files[].confidence | full, partial or none |
| files[].commits | Number of commits that contributed to W |
| symbols[] | node (`sym:path::name`), path, name, kind, lines [start, end], W, D, V |
| excluded[] | Path and exclusion reason |

**Invariance.** `tests/test_invariance.py` checks, on fixture repositories, that W behaves as work should under controlled changes: whitespace and mechanical reformats, renames, moving a function or splitting a file, re-adding a file in many small commits, squashing, reverting and re-applying, rewriting authors and messages, and clean merges leave it unchanged within tight tolerances; copying code adds little; a conflict resolved by hand at a merge credits only the resolved lines; a shallow clone gives null, never 0; and re-scanning gives byte-identical output. Code copied into an existing file from an unrelated file, and copies under about 40 characters, count as new work: documented limits.
