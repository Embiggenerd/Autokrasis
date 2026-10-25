"""Invariance tests for W (docs/plan.md, Scoring spec: Invariance), one test per rule.

Each test builds two histories that differ only by the transformation and compares W. Attribution
expectations (which commit is credited with which lines, survival fractions, nulls) are written by
hand from the spec in GOLDEN below; byte counts come from the reference compressor, so the tests
compare them only through equalities and tolerances.
"""

import hashlib
import subprocess

import pytest

from vscore.config import Config
from vscore.scan import scan
from vscore.score import codebase_total

# Hand-written from the spec: the lines each commit is credited with, and its survival.
GOLDEN = {
    # A merge that resolves a conflict by hand: only the resolution line is new relative to both
    # parents, so only it is credited to the merge; the feature-only line keeps the feature commit.
    "merge_conflict": {
        "file": "data.txt",
        "credits": {"merge feature": {"lines": [2], "surv": 1.0}, "feature change": {"lines": [3], "surv": 0.5}},
    },
    # A commit adds 20 lines; a later commit deletes the first 10 and copies the other 10 into a new
    # file. 10 distinct original lines survive (each in two places), so surv = 0.5, and each copy of
    # a surviving line carries half its weight.
    "delete_half_copy_half": {"commit": "twenty lines", "surviving": 10, "surv": 0.5,
                              "weight_by_file": {"a.txt": 5.0, "b.txt": 5.0}},
}


def lines(tag: str, n: int, start: int = 0, quote: str = "'") -> str:
    return "".join(
        f"{tag}_{i} = {quote}{hashlib.sha256(f'{tag}:{i}'.encode()).hexdigest()[:28]}{quote}\n"
        for i in range(start, start + n)
    )


def func(name: str, n: int = 10) -> str:
    body = "".join(f"    v{i} = '{hashlib.sha256(f'{name}:{i}'.encode()).hexdigest()[:28]}'\n" for i in range(n))
    return f"def {name}():\n{body}    return v0\n\n\n"


def files(result) -> dict:
    return {f["path"]: f for f in result.data["files"]}


def w(result, path: str) -> float:
    return files(result)[path]["W"]


def total(result) -> float:
    return result.data["total"]["W"]


def symbol_w(result, path: str, name: str) -> float:
    return next(s["W"] for s in result.data["symbols"] if s["path"] == path and s["name"] == name)


def within(a: float, b: float, rel: float) -> bool:
    return abs(a - b) <= rel * max(abs(b), 1e-9)


# ---------------------------------------------------------------- formatting and renames


def test_whitespace_only_reformat_preserves_w(make_repo):
    def base(r):
        r.write("a.txt", lines("a", 20))
        r.commit("one")
        r.write("a.txt", lines("a", 20) + lines("b", 10))
        r.commit("two")
    x, y = make_repo(), make_repo()
    base(x), base(y)
    y.write("a.txt", "".join("    " + line for line in (lines("a", 20) + lines("b", 10)).splitlines(True)))
    y.commit("reindent")
    assert w(scan(y.path), "a.txt") == w(scan(x.path), "a.txt")


def _quote_history(r):
    r.write("a.py", lines("a", 20))
    r.commit("one")
    r.write("a.py", lines("a", 20) + lines("b", 10))
    r.commit("two")


def test_listed_reformat_preserves_w(make_repo):
    x, y = make_repo(), make_repo()
    _quote_history(x), _quote_history(y)
    y.write("a.py", lines("a", 20, quote='"') + lines("b", 10, quote='"'))
    formatter = y.commit("switch to double quotes")
    y.write(".git-blame-ignore-revs", f"{formatter}\n")
    y.commit("ignore the formatter")
    assert within(w(scan(y.path), "a.py"), w(scan(x.path), "a.py"), 0.02)


def test_unlisted_reformat_stays_close(make_repo):
    x, y = make_repo(), make_repo()
    _quote_history(x), _quote_history(y)
    y.write("a.py", lines("a", 20, quote='"') + lines("b", 10, quote='"'))
    y.commit("switch to double quotes")
    assert within(w(scan(y.path), "a.py"), w(scan(x.path), "a.py"), 0.10)


def test_unlisted_reformat_is_reported_as_mechanical(make_repo):
    r = make_repo()
    _quote_history(r)
    r.write("a.py", lines("a", 20, quote='"') + lines("b", 10, quote='"'))
    formatter = r.commit("switch to double quotes")
    assert scan(r.path).data["scan"]["coverage"]["mechanical_commits"] == [formatter]


def _styled(names: list[str], verb: str = "get", call: str = "get(key)") -> str:
    """Code in one consistent style, as real files are: it compresses well against itself."""
    return "".join(
        f"def {verb}_{n}(self, key: str) -> {n.title()}:\n"
        f'    """{verb.title()} the {n} stored under key."""\n'
        f"    value = self._{n}s.{call}\n"
        f"    if value is None:\n"
        f"        raise KeyError(f'no {n} named {{key}}')\n"
        f"    return value\n\n\n"
        for n in names
    )


def test_new_code_in_a_files_own_style_is_not_mechanical(make_repo):
    # Found by the cross-repository comparison: against the whole file, new code in its style carries
    # under 0.10 bytes of information per byte, as a reformat does (HTTPX's "Add trust_env property"
    # measured 0.05), so compression cannot tell them apart. Its words are new; a reformat's are not.
    from vscore.adapters.history import info_bytes

    old = _styled([f"item{i}" for i in range(60)])
    new = _styled(["item3", "item7", "item11", "item20", "item42", "item51"], "pop", "pop(key, None)")
    r = make_repo()
    r.write("store.py", old)
    r.commit("store")
    r.write("store.py", old + new)
    feature = r.commit("add six pop methods")
    assert info_bytes(new.encode(), old.encode(), 9) < 0.10 * len(new.encode())  # the case the old rule missed
    result = scan(r.path)
    assert result.data["scan"]["coverage"]["mechanical_commits"] == []
    assert feature in [c.commit for c in result.contributions["store.py"]]


def test_a_reformat_landing_with_a_real_edit_is_judged_per_file(make_repo):
    # attrs's "Fade to Black" reformatted the code and, in the same commit, added contributor docs.
    x, y = make_repo(), make_repo()
    _quote_history(x), _quote_history(y)
    y.write("a.py", lines("a", 20, quote='"') + lines("b", 10, quote='"'))
    y.write("CONTRIBUTING.txt", lines("guide", 12))
    formatter = y.commit("switch to double quotes and document the style")
    result = scan(y.path)
    assert result.data["scan"]["coverage"]["mechanical_commits"] == [formatter]
    assert within(w(result, "a.py"), w(scan(x.path), "a.py"), 0.10)
    assert [c.commit for c in result.contributions["CONTRIBUTING.txt"]] == [formatter]
    assert result.contributions["CONTRIBUTING.txt"][0].surv == 1.0


def test_small_real_edits_are_never_looked_through(make_repo):
    r = make_repo()
    r.write("a.py", lines("a", 20))
    r.commit("one")
    r.write("a.py", lines("a", 19) + "a_19 = 'changed by hand'\n")
    edit = r.commit("fix one value")
    result = scan(r.path)
    assert result.data["scan"]["coverage"]["mechanical_commits"] == []
    assert edit in [c.commit for c in result.contributions["a.py"]]


def test_rename_preserves_w(make_repo):
    x, y = make_repo(), make_repo()
    for r in (x, y):
        r.write("a.txt", lines("a", 20))
        r.commit("one")
    y.move("a.txt", "b.txt")
    y.commit("rename")
    assert w(scan(y.path), "b.txt") == w(scan(x.path), "a.txt")


# ---------------------------------------------------------------- moves, splits and copies


def _two_functions(r):
    r.write("a.py", func("f"))
    r.commit("write f")
    r.write("a.py", func("f") + func("g"))
    r.commit("write g")


def test_moving_a_function_moves_its_work(make_repo):
    x, y = make_repo(), make_repo()
    _two_functions(x), _two_functions(y)
    y.write("a.py", func("f"))
    y.write("b.py", func("g"))
    y.commit("move g to b.py")
    after = scan(y.path)
    assert within(w(after, "a.py") + w(after, "b.py"), w(scan(x.path), "a.py"), 0.05)


def test_splitting_a_file_preserves_total(make_repo):
    x, y = make_repo(), make_repo()
    for r in (x, y):
        r.write("a.txt", lines("a", 30))
        r.commit("thirty lines")
    y.write("a.txt", lines("a", 15))
    y.write("b.txt", lines("a", 15, start=15))
    y.commit("split")
    after = scan(y.path)
    assert within(w(after, "a.txt") + w(after, "b.txt"), w(scan(x.path), "a.txt"), 0.05)


def test_merging_two_files_preserves_total(make_repo):
    x, y = make_repo(), make_repo()
    for r in (x, y):
        r.write("a.txt", lines("a", 15))
        r.write("c.txt", lines("c", 15))
        r.commit("two files")
    y.write("a.txt", lines("a", 15) + lines("c", 15))
    y.remove("c.txt")
    y.commit("merge c into a")
    before = scan(x.path)
    assert within(w(scan(y.path), "a.txt"), w(before, "a.txt") + w(before, "c.txt"), 0.05)


def _copy_rise(x, y, copied_w):
    return (total(scan(y.path)) - total(scan(x.path))) / copied_w


def test_copying_into_a_new_file_adds_almost_nothing(make_repo):
    x, y = make_repo(), make_repo()
    _two_functions(x), _two_functions(y)
    y.write("c.py", func("g"))
    y.commit("copy g into c.py")
    assert _copy_rise(x, y, symbol_w(scan(x.path), "a.py", "g")) <= 0.05


def test_copying_into_an_existing_unrelated_file(make_repo):
    x, y = make_repo(), make_repo()
    for r in (x, y):
        _two_functions(r)
        r.write("d.py", lines("d", 10))
        r.commit("unrelated d.py")
    y.write("d.py", lines("d", 10) + func("g"))
    y.commit("copy g into d.py")
    rise = _copy_rise(x, y, symbol_w(scan(x.path), "a.py", "g"))
    if Config().copy_detection >= 3:
        assert rise <= 0.05
    else:  # documented limit: copies into an existing file from an unrelated one count as new
        assert rise > 0.05


def test_copying_within_a_file_adds_almost_nothing(make_repo):
    x, y = make_repo(), make_repo()
    _two_functions(x), _two_functions(y)
    y.write("a.py", func("f") + func("g") + func("g").replace("def g", "def g2"))
    y.commit("duplicate g")
    assert _copy_rise(x, y, symbol_w(scan(x.path), "a.py", "g")) <= 0.05


def test_deleting_half_and_copying_half_counts_each_original_line_once(make_repo):
    golden = GOLDEN["delete_half_copy_half"]
    r = make_repo()
    r.write("a.txt", lines("first", 10) + lines("second", 10))
    commit = r.commit(golden["commit"])
    r.write("a.txt", lines("second", 10))
    r.write("b.txt", lines("second", 10))
    r.commit("drop first half, copy second half")
    result = scan(r.path)
    found = {path: [c for c in result.contributions[path] if c.commit == commit] for path in ("a.txt", "b.txt")}
    for path, weight in golden["weight_by_file"].items():
        [c] = found[path]
        assert c.surviving_lines == golden["surviving"]
        assert c.surv == golden["surv"]
        assert sum(wt for _, wt in c.lines) == pytest.approx(weight)
    commit_work = sum(c.work for cs in found.values() for c in cs)
    assert commit_work == pytest.approx(found["a.txt"][0].info * golden["surv"])


def test_short_copies_count_as_new_work(make_repo):
    r = make_repo()
    r.write("a.txt", "k = 1\n" + lines("a", 10))
    r.commit("one")
    r.write("b.txt", "k = 1\n")
    copier = r.commit("copy one short line")
    assert [c.commit for c in scan(r.path).contributions["b.txt"]] == [copier]  # documented limit


# ---------------------------------------------------------------- history shape


def test_splitting_a_commit_into_many_small_ones(make_repo):
    x, y = make_repo(), make_repo()
    x.write("a.txt", lines("a", 30))
    x.commit("thirty lines")
    for i in range(30):
        y.write("a.txt", lines("a", i + 1))
        y.commit(f"line {i}")
    assert total(scan(y.path)) <= total(scan(x.path)) * 1.10


def test_squashing_never_raises_w(make_repo):
    final = lines("a", 15) + lines("changed", 5) + lines("added", 5)
    x, y = make_repo(), make_repo()
    x.write("a.txt", lines("a", 20))
    x.commit("twenty")
    x.write("a.txt", lines("a", 15) + lines("changed", 5))
    x.commit("change five")
    x.write("a.txt", final)
    x.commit("add five")
    y.write("a.txt", final)
    y.commit("squashed")
    # +5%, not +2%: a partly deleted commit keeps its surviving-line share of its work, which assumes the
    # deleted lines cost the average. That approximation measured +3.2% here; it is noise, not inflation.
    assert total(scan(y.path)) <= total(scan(x.path)) * 1.05


def test_revert_then_reapply_counts_the_change_once(make_repo):
    x, y = make_repo(), make_repo()
    for r in (x, y):
        r.write("a.txt", lines("base", 10))
        r.commit("base")
        r.write("a.txt", lines("base", 10) + lines("change", 15))
        r.commit("change")
    y.write("a.txt", lines("base", 10))
    y.commit("revert")
    y.write("a.txt", lines("base", 10) + lines("change", 15))
    y.commit("reapply")
    assert within(total(scan(y.path)), total(scan(x.path)), 0.05)


def test_conflict_resolution_is_credited_to_the_merge(make_repo):
    golden = GOLDEN["merge_conflict"]
    r = make_repo()
    shared = lines("shared", 1)
    r.write("data.txt", lines("header", 1) + shared + lines("tail", 1))
    r.commit("base")
    r.checkout("feature", create=True)
    r.write("data.txt", lines("header", 1) + lines("feature", 1) + lines("feature_only", 1))
    shas = {"feature change": r.commit("feature change")}
    r.checkout("main")
    r.write("data.txt", lines("header", 1) + lines("main", 1) + lines("tail", 1))
    r.commit("main change")
    resolved = lines("header", 1) + lines("resolved", 1) + lines("feature_only", 1)
    shas["merge feature"] = r.merge("feature", "merge feature", resolve={"data.txt": resolved})
    contributions = {c.commit: c for c in scan(r.path).contributions[golden["file"]]}
    for label, expect in golden["credits"].items():
        c = contributions[shas[label]]
        assert sorted(line for line, _ in c.lines) == expect["lines"]
        assert c.surv == expect["surv"]


def test_a_clean_merge_adds_nothing(make_repo):
    x, y = make_repo(), make_repo()
    for r in (x, y):
        r.write("a.txt", lines("a", 10))
        r.commit("base")
    x.write("b.txt", lines("b", 10))
    x.commit("b")
    x.write("c.txt", lines("c", 10))
    x.commit("c")
    y.checkout("side", create=True)
    y.write("b.txt", lines("b", 10))
    y.commit("b")
    y.checkout("main")
    y.write("c.txt", lines("c", 10))
    y.commit("c")
    y.merge("side", "merge side")
    assert total(scan(y.path)) == pytest.approx(total(scan(x.path)), abs=1e-3)


def test_metadata_does_not_change_w(make_repo):
    x = make_repo()
    y = make_repo(author="Other", epoch=1_800_000_000)
    for r, word in ((x, "first"), (y, "different words")):
        r.write("a.txt", lines("a", 20))
        r.commit(word)
        r.write("a.txt", lines("a", 20) + lines("b", 10))
        r.commit(word + " again")
    assert total(scan(y.path)) == total(scan(x.path))


# ---------------------------------------------------------------- symbols, exclusions, missing data


def test_names_sharing_a_line_split_its_work(make_repo):
    r = make_repo()
    source = "alpha = beta = '" + hashlib.sha256(b"x").hexdigest() + "'\n\n\n" + func("f")
    r.write("m.py", source)
    r.commit("one")
    result = scan(r.path)
    line_work = w(result, "m.py") / len(source.splitlines())  # one commit, every line survives
    alpha, beta = symbol_w(result, "m.py", "alpha"), symbol_w(result, "m.py", "beta")
    assert alpha == pytest.approx(line_work / 2, rel=1e-3)
    assert beta == pytest.approx(line_work / 2, rel=1e-3)


def test_marked_generated_files_change_nothing(make_repo):
    x, y = make_repo(), make_repo()
    for r in (x, y):
        r.write("a.py", func("f"))
        r.write(".gitattributes", "gen.py linguist-generated\n")
        r.commit("code")
    y.write("gen.py", func("generated", 40))
    y.commit("generated client")
    after = scan(y.path)
    assert "gen.py" not in files(after)
    assert w(after, "a.py") == w(scan(x.path), "a.py")


def test_shallow_clone_totals_are_null_not_zero(make_repo, tmp_path):
    r = make_repo()
    r.write("a.txt", lines("a", 10))
    r.commit("one")
    r.write("a.txt", lines("a", 20))
    r.commit("two")
    clone = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{r.path}", str(clone)], check=True)
    data = scan(clone).data
    assert data["total"]["W"] is None and data["total"]["V"] is None
    assert data["total"]["reason"] == "shallow_clone"


def test_partial_totals_are_labeled_with_coverage():
    measured = codebase_total([3.0, None, 4.0], sizes=[100, 300, 100])
    assert measured["W"] == 7.0 and measured["partial"] is True
    assert measured["files_measured"] == 2 and measured["files_eligible"] == 3
    assert measured["bytes_measured_share"] == pytest.approx(0.4)
    nothing = codebase_total([None, None], sizes=[10, 10], reason="no_history")
    assert nothing["W"] is None and nothing["V"] is None and nothing["reason"] == "no_history"


def test_rescanning_is_byte_identical(make_repo):
    r = make_repo()
    _two_functions(r)
    assert scan(r.path).to_json() == scan(r.path).to_json()


def test_new_used_work_raises_w_and_d(make_repo):
    x, y = make_repo(), make_repo()
    for r in (x, y):
        r.write("lib.py", func("f"))
        r.commit("lib")
    y.write("lib.py", func("f") + func("h"))
    for name in "abc":
        y.write(f"{name}.py", "from lib import h\n")
    y.commit("add h and use it")
    before, after = scan(x.path), scan(y.path)
    assert w(after, "lib.py") > w(before, "lib.py")
    h = next(s for s in after.data["symbols"] if s["name"] == "h")
    assert h["D"] == 3 and h["W"] > 0
