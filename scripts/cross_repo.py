"""The cross-repository comparison of the scorer release (docs/plan.md, "Reliable scorer release").

Usage:
  python scripts/cross_repo.py survey <out.json> <repo-path>:<name> ...
  python scripts/cross_repo.py transform <out.json> <work-dir> <repo-path>:<name> ...

`survey` scans each repository cold at HEAD and again 50 first-parent commits earlier, and reports
runtime, coverage, the distribution of file V and how stable the ranking is between the two.

`transform` applies the invariance table's transformations to fresh clones of each repository, as new
commits on top of HEAD, and checks each row's rule against a scan of the untouched clone. Rows that need
a history built for them (deleting half of a commit's lines, sub-40-character copies, `a = b = 1`) stay
fixture-only. The clones go under <work-dir> and share the source repository's scan cache
(.git/vscore-cache); the source repositories are otherwise never modified.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from vscore import FORMULA_VERSION
from vscore.adapters.symbols import python_symbols
from vscore.scan import scan

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Transform", "GIT_AUTHOR_EMAIL": "transform@example.invalid",
    "GIT_COMMITTER_NAME": "Transform", "GIT_COMMITTER_EMAIL": "transform@example.invalid",
    "GIT_AUTHOR_DATE": "2026-10-24T12:00:00Z", "GIT_COMMITTER_DATE": "2026-10-24T12:00:00Z",
    "FILTER_BRANCH_SQUELCH_WARNING": "1",
}


def git(repo: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], env=ENV, capture_output=True, text=True)
    if check and done.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def timed_scan(repo: Path, rev: str = "HEAD", cold: bool = False):
    if cold:
        shutil.rmtree(repo / ".git" / "vscore-cache", ignore_errors=True)
    start = time.perf_counter()
    result = scan(repo, rev)
    return result, time.perf_counter() - start


def files(result) -> dict:
    return {f["path"]: f for f in result.data["files"]}


def total(result) -> float | None:
    return result.data["total"]["W"]


def symbol(result, path: str, name: str) -> dict | None:
    return next((s for s in result.data["symbols"] if s["path"] == path and s["name"] == name), None)


def ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3:
        return None
    ra, rb = ranks(a), ranks(b)
    ma, mb = statistics.fmean(ra), statistics.fmean(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    var = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return cov / var if var else None


def quantiles(values: list[float]) -> dict:
    values = sorted(values)
    pick = lambda q: values[min(len(values) - 1, int(q * (len(values) - 1) + 0.5))]  # noqa: E731
    return {"n": len(values), "min": values[0], "p25": pick(0.25), "median": pick(0.5), "p75": pick(0.75),
            "p90": pick(0.9), "max": values[-1]}


# ---------------------------------------------------------------- survey


def survey(path: Path, name: str) -> dict:
    head, seconds = timed_scan(path, cold=True)
    old_rev = git(path, "rev-parse", "HEAD~50")
    old, _ = timed_scan(path, old_rev)
    cov = head.data["scan"]["coverage"]
    eligible = cov["eligible"] or 1
    v_now = {p: f["V"] for p, f in files(head).items() if f["V"] is not None}
    v_old = {p: f["V"] for p, f in files(old).items() if f["V"] is not None}
    common = sorted(set(v_now) & set(v_old))
    top = lambda v: {p for p, _ in sorted(v.items(), key=lambda kv: -kv[1])[:10]}  # noqa: E731
    subjects = {sha: git(path, "log", "-1", "--format=%h %ad %s", "--date=short", sha)
                for sha in cov["mechanical_commits"]}
    return {
        "name": name,
        "revision": head.data["scan"]["revision"],
        "commits": int(git(path, "rev-list", "--count", "HEAD")),
        "merges": int(git(path, "rev-list", "--count", "--merges", "HEAD")),
        "cold_scan_seconds": round(seconds, 1),
        "eligible": cov["eligible"],
        "code_files": cov["code_files"],
        "W_null_share": round(cov["W_null"] / eligible, 3),
        "D_null_share": round(cov["D_null"] / eligible, 3),
        "V_null_share": round(cov["V_null"] / eligible, 3),
        "imports": cov["imports"],
        "imports_unresolved_share": round(cov["imports_unresolved"] / cov["imports"], 3) if cov["imports"] else None,
        "parse_errors": cov["parse_errors"],
        "mechanical_commits": [subjects[s] for s in cov["mechanical_commits"]],
        "total": head.data["total"],
        "V": quantiles(list(v_now.values())) if v_now else None,
        "symbols_scored": sum(s["V"] is not None for s in head.data["symbols"]),
        "stability": {
            "old_revision": old_rev,
            "files_compared": len(common),
            "spearman_V": round(r, 3) if (r := spearman([v_now[p] for p in common], [v_old[p] for p in common]))
            is not None else None,
            "top10_overlap": len(top(v_now) & top(v_old)),
        },
    }


# ---------------------------------------------------------------- transformations


def module_name(path: str) -> str:
    stem = path.removeprefix("src/").removesuffix(".py").replace("/", ".")
    return stem.removesuffix(".__init__")


def import_end(source: str) -> int:
    """Line after the module's leading docstring and imports, where a new import can go."""
    tree = ast.parse(source)
    end = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)) or (
                isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and end == 0):
            end = node.end_lineno
        else:
            break
    return end


def swap_quotes(source: str) -> tuple[str, int]:
    """Swaps the dominant string quote on every line where that is safe, keeping the file parseable."""
    q, other = ('"', "'") if source.count('"') >= source.count("'") else ("'", '"')
    lines = source.splitlines(True)
    changed = 0
    for i, line in enumerate(lines):
        if q * 3 in line or other in line or "\\" in line or line.count(q) < 2 or line.count(q) % 2:
            continue
        candidate = lines[:i] + [line.replace(q, other)] + lines[i + 1:]
        try:
            ast.parse("".join(candidate))
        except SyntaxError:
            continue
        lines, changed = candidate, changed + 1
    return "".join(lines), changed


class Targets:
    """The files and function each transformation works on, chosen from the base scan."""

    def __init__(self, repo: Path, base):
        self.repo = repo
        code = [f for f in base.data["files"] if f["class"] == "code" and f["path"].endswith(".py")
                and not f["path"].endswith("__init__.py") and f["W"]]
        code.sort(key=lambda f: -f["W"])
        self.F, self.G = code[0]["path"], code[1]["path"]
        sizes = {f["path"]: len(self.read(f["path"]).splitlines()) for f in code[2:]}
        mid = [p for p, n in sizes.items() if 80 <= n <= 400] or [p for p, n in sizes.items() if n >= 20]
        self.F2 = mid[0] if mid else self.G  # the file re-added in one commit or many
        funcs = [s for s in base.data["symbols"] if s["path"] == self.F and s["kind"] == "function" and s["W"]]
        if not funcs:
            funcs = [s for s in base.data["symbols"] if s["path"] == self.F and s["W"]]
        self.g = max(funcs, key=lambda s: s["W"])
        importers = set(base.importers.get(self.F, ()))
        self.non_importers = [f["path"] for f in code[1:] if f["path"] not in importers][:3]

    def read(self, path: str) -> str:
        return (self.repo / path).read_text()

    def write(self, path: str, text: str) -> None:
        (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / path).write_text(text)

    def g_text(self) -> str:
        a, b = self.g["lines"]
        return "".join(self.read(self.F).splitlines(True)[a - 1:b])


def contributions_by(result, sha: str) -> list:
    return [c for parts in result.contributions.values() if parts for c in parts if c.commit == sha]


def row(name: str, rule: str, measured: str, holds: bool | None) -> dict:
    return {"row": name, "rule": rule, "measured": measured, "holds": holds}


def pct(a: float, b: float) -> str:
    return f"{(a - b) / b * 100:+.2f}%" if b else "n/a"


def transform(src: Path, name: str, work: Path) -> dict:
    root = work / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)

    cache = (src / ".git" / "vscore-cache").resolve()
    cache.mkdir(exist_ok=True)

    def fresh(label: str) -> Path:
        path = root / label
        subprocess.run(["git", "clone", "-q", str(src), str(path)], env=ENV, check=True)
        # Every clone shares the source repository's scan cache: entries are keyed by commit and written
        # atomically, so they are valid in any clone, and a repeated run starts warm.
        (path / ".git" / "vscore-cache").symlink_to(cache)
        return path

    base_repo = fresh("base")
    base, base_seconds = timed_scan(base_repo)
    t = Targets(base_repo, base)
    F, G, F2, g = t.F, t.G, t.F2, t.g
    WF, WG, Wg, WT = files(base)[F]["W"], files(base)[G]["W"], g["W"], total(base)
    rows: list[dict] = []

    def run(label: str, edit):
        repo = fresh(label)
        tt = Targets.__new__(Targets)
        tt.__dict__.update(t.__dict__, repo=repo)
        extra = edit(repo, tt)
        return scan(repo), extra

    # Whitespace-only reformat: trailing spaces on every non-blank line.
    def ws(repo, tt):
        tt.write(F, "".join(l[:-1] + "  \n" if l.strip() and l.endswith("\n") else l
                            for l in tt.read(F).splitlines(True)))
        commit(repo, "whitespace")

    # Quote swap, listed in .git-blame-ignore-revs and not.
    def quotes(listed):
        def edit(repo, tt):
            text, n = swap_quotes(tt.read(F))
            tt.write(F, text)
            sha = commit(repo, "swap quotes")
            if listed:
                ignore = repo / ".git-blame-ignore-revs"
                ignore.write_text((ignore.read_text() if ignore.exists() else "") + sha + "\n")
                commit(repo, "list the reformat")
            return n, sha
        return edit

    renamed = F.removesuffix(".py") + "_renamed.py"

    def rename(repo, tt):
        git(repo, "mv", F, renamed)
        commit(repo, "rename")

    def move(repo, tt):  # a function to another file
        a, b = g["lines"]
        body = tt.g_text()
        lines = tt.read(F).splitlines(True)
        tt.write(F, "".join(lines[:a - 1] + lines[b:]))
        tt.write(G, tt.read(G).rstrip("\n") + "\n\n\n" + body)
        commit(repo, "move function")

    part2 = F.removesuffix(".py") + "_part2.py"

    def split(repo, tt):  # at the top-level symbol nearest the middle
        lines = tt.read(F).splitlines(True)
        cut = min((s.start for s in python_symbols("".join(lines))), key=lambda s: abs(s - len(lines) / 2))
        tt.write(F, "".join(lines[:cut - 1]))
        tt.write(part2, "".join(lines[cut - 1:]))
        commit(repo, "split file")

    copy_path = str(Path(F).parent / f"copy_of_{g['name']}.py")

    def copy_new(repo, tt):
        tt.write(copy_path, tt.g_text())
        commit(repo, "copy into new file")

    def copy_same(repo, tt):
        tt.write(F, tt.read(F).rstrip("\n") + "\n\n\n" + tt.g_text())  # an exact copy: renaming it is new work
        commit(repo, "copy within file")

    def copy_existing(repo, tt):
        tt.write(G, tt.read(G).rstrip("\n") + "\n\n\n" + tt.g_text())
        commit(repo, "copy into existing file")

    # Split one commit into many: delete F2, then re-add it in one commit or in 10-line commits.
    def readd(chunked):
        def edit(repo, tt):
            text = tt.read(F2)
            (repo / F2).unlink()
            commit(repo, "delete")
            lines = text.splitlines(True)
            step = 10 if chunked else len(lines)
            for i in range(0, len(lines), step):
                tt.write(F2, "".join(lines[:i + step]))
                commit(repo, f"re-add lines {i + 1}+")
        return edit

    def squash(repo, tt):  # the last 20 first-parent commits into one
        git(repo, "reset", "-q", "--soft", "HEAD~20")
        git(repo, "commit", "-q", "--no-verify", "-m", "squash")

    def revert(repo, tt):  # a recent change, then re-apply it
        for sha in git(repo, "log", "--no-merges", "--format=%H", "-n", "40", "--", "*.py").split():
            done = subprocess.run(["git", "-C", str(repo), "revert", "--no-edit", sha], env=ENV, capture_output=True)
            if done.returncode == 0:
                git(repo, "revert", "--no-edit", "HEAD")
                return sha
            git(repo, "revert", "--abort", check=False)
        raise RuntimeError("no revertible commit")

    def metadata(repo, tt):  # every commit's author, committer, dates and message
        stream = subprocess.run(["git", "-C", str(repo), "fast-export", "--all", "--signed-tags=strip"],
                                env=ENV, capture_output=True, check=True).stdout
        out, pos, in_commit = bytearray(), 0, False
        while pos < len(stream):
            end = stream.index(b"\n", pos) + 1
            line, pos = stream[pos:end], end
            if line.startswith(b"commit "):
                in_commit = True
            elif line.startswith((b"author ", b"committer ")):
                line = line.split(b" ", 1)[0] + b" Someone <someone@example.invalid> 978307200 +0000\n"
            elif line.startswith(b"data "):
                n = int(line[5:])
                payload, pos = stream[pos:pos + n], pos + n
                if in_commit:
                    payload, in_commit = b"rewritten: " + payload, False
                line = b"data %d\n" % len(payload) + payload
            out += line
        subprocess.run(["git", "-C", str(repo), "fast-import", "--force", "--quiet"], input=bytes(out),
                       env=ENV, capture_output=True, check=True)
        return git(repo, "rev-parse", "HEAD")

    def generated(repo, tt):  # generated, vendored and minified files, marked or matching exclusion patterns
        text = tt.read(F)
        tt.write("vendor/vendored_copy.py", text)
        tt.write("generated_code.py", text)
        tt.write("static/bundle.min.js", text)
        attrs = repo / ".gitattributes"
        attrs.write_text((attrs.read_text() if attrs.exists() else "") + "generated_code.py linguist-generated\n")
        commit(repo, "add generated files")

    probe = "vscore_probe_function"

    def new_function(repo, tt):  # a new, original function that other files import
        body = "".join(f"    step_{i} = 'probe {i} {hashlib.sha256(f'{name}:{i}'.encode()).hexdigest()[:12]}'\n"
                       for i in range(15))
        tt.write(F, tt.read(F).rstrip("\n") + f"\n\n\ndef {probe}():\n{body}    return step_0\n")
        for path in tt.non_importers:
            lines = tt.read(path).splitlines(True)
            at = import_end("".join(lines))
            tt.write(path, "".join(lines[:at] + [f"from {module_name(F)} import {probe}\n"] + lines[at:]))
        commit(repo, "new function")

    def merge(conflict):  # clean, or with a conflict resolved by hand
        def edit(repo, tt):
            git(repo, "checkout", "-q", "-b", "side")
            if conflict:
                lines = tt.read(F).splitlines(True)
                a, b = g["lines"]
                simple = re.compile(r"\s+(\w+ = |return\b)[^'\"\\]*$")  # a statement a comment can end safely
                at = next((i for i in [*range(a, b), *range(len(lines))] if simple.match(lines[i])))
                original = lines[at].rstrip("\n")
                lines[at] = original + "  # side\n"
                tt.write(F, "".join(lines))
            else:
                tt.write(G, tt.read(G) + "\n\nSIDE_ONLY_VALUE = 'written on the side branch'\n")
            commit(repo, "side change")
            git(repo, "checkout", "-q", "-")
            if conflict:
                lines[at] = original + "  # main\n"
                tt.write(F, "".join(lines))
            else:
                tt.write(F, tt.read(F) + "\n\nMAIN_ONLY_VALUE = 'written on the main branch'\n")
            commit(repo, "main change")
            git(repo, "merge", "--no-ff", "-q", "-m", "merge side", "side", check=not conflict)
            if conflict:
                lines[at] = original + "  # resolved by hand\n"
                tt.write(F, "".join(lines))
                commit(repo, "merge side")
            return git(repo, "rev-parse", "HEAD")
        return edit

    def small_edit(repo, tt):  # one changed value
        lines = tt.read(F).splitlines(True)
        at = next(i for i, l in enumerate(lines) if re.search(r"(?<![\w.])\d+(?![\w.])", l)
                  and not l.lstrip().startswith("#"))
        lines[at] = re.sub(r"(?<![\w.])(\d+)(?![\w.])", lambda m: str(int(m.group(1)) + 1), lines[at], count=1)
        tt.write(F, "".join(lines))
        return commit(repo, "change one value")

    def shallow_scan():
        shallow = root / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{base_repo}", str(shallow)], env=ENV, check=True)
        return scan(shallow), None

    edits = {
        "whitespace": ws, "quotes-listed": quotes(True), "quotes-unlisted": quotes(False), "rename": rename,
        "move-function": move, "split-file": split, "copy-new-file": copy_new, "copy-same-file": copy_same,
        "copy-existing-file": copy_existing, "readd-one": readd(False), "readd-many": readd(True),
        "squash": squash, "revert-reapply": revert, "metadata": metadata, "generated": generated,
        "new-function": new_function, "merge-clean": merge(False), "merge-conflict": merge(True),
        "small-edit": small_edit,
    }
    with ThreadPoolExecutor(os.cpu_count() or 4) as pool:  # each row works in its own clone
        futures = {label: pool.submit(run, label, edit) for label, edit in edits.items()}
        futures["shallow"] = pool.submit(shallow_scan)
        futures["rescan"] = pool.submit(lambda: (scan(base_repo), None))
        done = {label: future.result() for label, future in futures.items()}

    r, _ = done["whitespace"]
    rows.append(row("Whitespace-only reformat", "File W and total exact",
                    f"file {pct(files(r)[F]['W'], WF)}, total {pct(total(r), WT)}",
                    files(r)[F]["W"] == WF and total(r) == WT))
    r, (n, sha) = done["quotes-listed"]
    rows.append(row("Non-whitespace reformat, listed", "File W within ±3% (±2% on fixtures)",
                    f"{n} lines changed; file {pct(files(r)[F]['W'], WF)}",
                    abs(files(r)[F]["W"] - WF) <= 0.03 * WF))
    r, (n, sha) = done["quotes-unlisted"]
    mech = sha in r.data["scan"]["coverage"]["mechanical_commits"]
    rows.append(row("The same reformat, not listed", "File W within ±10%",
                    f"{n} lines changed; file {pct(files(r)[F]['W'], WF)}; detected as mechanical: {mech}",
                    abs(files(r)[F]["W"] - WF) <= 0.10 * WF))
    r, _ = done["rename"]
    rows.append(row("Rename or move a file", "File W and total exact",
                    f"file {pct(files(r)[renamed]['W'], WF)}, total {pct(total(r), WT)}",
                    files(r)[renamed]["W"] == WF and total(r) == WT))
    r, _ = done["move-function"]
    moved = symbol(r, G, g["name"])
    rows.append(row("Move a function to another file", "Total W within ±5%; W follows the function",
                    f"total {pct(total(r), WT)}; function W {pct(moved['W'], Wg)} in its new file",
                    abs(total(r) - WT) <= 0.05 * WT and abs(moved["W"] - Wg) <= 0.05 * Wg))
    r, _ = done["split-file"]
    both = files(r)[F]["W"] + files(r)[part2]["W"]
    rows.append(row("Split a file in two", "W over the two files within ±5% of the original",
                    f"files {pct(both, WF)}, total {pct(total(r), WT)}", abs(both - WF) <= 0.05 * WF))
    for label, name_, rule in (("copy-new-file", "Copy code into a new file", "Rise at most 5% of the copied code's W"),
                               ("copy-same-file", "Copy code within the same file",
                                "Rise at most 5% of the copied code's W"),
                               ("copy-existing-file", "Copy code into an existing unrelated file",
                                "Documented limit with -C -C")):
        rise = total(done[label][0]) - WT
        rows.append(row(name_, rule, f"rise {rise / Wg * 100:.1f}% of the function's W",
                        None if label == "copy-existing-file" else rise <= 0.05 * Wg))
    w1, wn = files(done["readd-one"][0])[F2]["W"], files(done["readd-many"][0])[F2]["W"]
    rows.append(row("Split one commit into many small ones", "W at most +10%",
                    f"{len(t.read(F2).splitlines())} lines in 10-line commits: {pct(wn, w1)}", wn <= 1.10 * w1))
    r, _ = done["squash"]
    rows.append(row("Squash a branch into one commit", "Total W at most +5%",
                    f"last 20 first-parent commits: total {pct(total(r), WT)}", total(r) <= 1.05 * WT))
    r, sha = done["revert-reapply"]
    rows.append(row("Revert a change, then re-apply it", "Total W within ±5%",
                    f"commit {sha[:8]}: total {pct(total(r), WT)}", abs(total(r) - WT) <= 0.05 * WT))
    r, _ = done["metadata"]
    rows.append(row("Rewrite authors, dates or messages", "Total W exact",
                    f"every commit rewritten: total {pct(total(r), WT)}", total(r) == WT))
    r, _ = done["generated"]
    attr_w = lambda res: (files(res).get(".gitattributes") or {}).get("W") or 0  # noqa: E731
    rest = total(r) - attr_w(r)
    rows.append(row("Add generated, vendored or minified files", "Total W (other than .gitattributes) exact",
                    f"total {pct(rest, WT - attr_w(base))}", rest == WT - attr_w(base)))
    r, _ = done["new-function"]
    s = symbol(r, F, probe)
    k = len(t.non_importers)
    rows.append(row("Add a new, original function that other files import",
                    f"W and D of its file rise; the function has W > 0 and D = {k}",
                    f"file W {pct(files(r)[F]['W'], WF)}, D {files(base)[F]['D']} → {files(r)[F]['D']}; "
                    f"function W {s['W']}, D {s['D']}",
                    files(r)[F]["W"] > WF and files(r)[F]["D"] > files(base)[F]["D"] and s["W"] > 0 and s["D"] == k))
    r, sha = done["merge-clean"]
    credited = contributions_by(r, sha)
    rows.append(row("Merge cleanly", "The merge is credited with nothing",
                    f"merge credited with {sum(c.here for c in credited):g} lines", not any(c.here for c in credited)))
    r, sha = done["merge-conflict"]
    credited = contributions_by(r, sha)
    lines_here = sum(c.here for c in credited)
    rows.append(row("Resolve a conflict by hand in a merge", "The merge is credited with exactly the resolved line",
                    f"merge credited with {lines_here:g} line(s), surv {[c.surv for c in credited]}",
                    lines_here == 1 and all(c.surv == 1.0 for c in credited)))
    r, sha = done["small-edit"]
    mech = sha in r.data["scan"]["coverage"]["mechanical_commits"]
    credited = sum(c.work for c in contributions_by(r, sha))
    rows.append(row("A small real edit (one changed value)", "Not mechanical, and credited",
                    f"mechanical: {mech}; credited W {credited:.1f}", not mech and credited > 0))
    r, _ = done["shallow"]
    rows.append(row("Scan a shallow clone with default settings", "W and totals null, never 0",
                    f"total W {r.data['total']['W']}, reason {r.data['total'].get('reason')}",
                    r.data["total"]["W"] is None and all(f["W"] is None for f in r.data["files"])))
    again, _ = done["rescan"]
    rows.append(row("Re-scan the same inputs", "Byte-identical output",
                    "identical" if again.to_json() == base.to_json() else "differs",
                    again.to_json() == base.to_json()))

    return {"name": name, "revision": base.data["scan"]["revision"], "base_scan_seconds": round(base_seconds, 1),
            "targets": {"file": F, "other_file": G, "readded_file": F2, "function": g["name"],
                        "function_lines": g["lines"], "importers_added": t.non_importers},
            "base": {"total_W": WT, "file_W": WF, "function_W": Wg, "other_file_W": WG},
            "rows": rows}


def main(argv: list[str]) -> int:
    mode, out = argv[0], Path(argv[1])
    if mode == "survey":
        pairs, results = argv[2:], []
        for pair in pairs:
            path, name = pair.rsplit(":", 1)
            results.append(survey(Path(path), name))
            print(json.dumps(results[-1], indent=1), flush=True)
    elif mode == "transform":
        work, pairs = Path(argv[2]), [pair.rsplit(":", 1) for pair in argv[3:]]
        with ThreadPoolExecutor(len(pairs)) as pool:  # repositories in parallel, as well as their rows
            results = list(pool.map(lambda pair: transform(Path(pair[0]), pair[1], work), pairs))
        for result in results:
            for item in result["rows"]:
                print(f"{result['name']}: {'HOLDS' if item['holds'] else 'FAILS' if item['holds'] is False else 'LIMIT'}"
                      f" {item['row']}: {item['measured']}", flush=True)
    else:
        raise SystemExit(__doc__)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"formula": FORMULA_VERSION, mode: results}, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
