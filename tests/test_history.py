import subprocess

from vscore.adapters.history import info_bytes, parse_blame, parse_diff, unquote
from vscore.scan import scan

LINES = "".join(f"value_{i} = compute({i}, scale={i * 7 % 13})\n" for i in range(20))


def contributions(repo, path, **kwargs):
    result = scan(repo.path, **kwargs)
    return {c.commit: c for c in result.contributions[path]}, result


def test_info_rewards_new_content_over_repetition():
    reference = "".join(f"def handler_{i}(request):\n    return render(request, 'page_{i}.html')\n" for i in range(40))
    boilerplate = b"def handler_40(request):\n    return render(request, 'page_40.html')\n"
    novel = b"def settle(ledger, fx):\n    return sum(v * fx.get(k, 1.0) for k, v in ledger.items() if v > 0)\n"
    assert info_bytes(boilerplate, reference.encode(), 9) < info_bytes(novel, reference.encode(), 9)
    assert info_bytes(b"", reference.encode(), 9) == 0
    assert info_bytes(novel, b"", 9) > 0


def test_unquote_handles_git_c_quoting():
    assert unquote(b"plain/path.py") == "plain/path.py"
    assert unquote(b'"we\\"ird\\tname.py"') == 'we"ird\tname.py'
    assert unquote(b'"caf\\303\\251.py"') == "café.py"


def test_parse_diff_pairs_renames_and_collects_added_lines():
    output = (
        b":100644 100644 " + b"1" * 40 + b" " + b"2" * 40 + b" R091\told.py\tnew.py\n"
        b":000000 100644 " + b"0" * 40 + b" " + b"3" * 40 + b" A\tfresh.py\n"
        b":100644 000000 " + b"4" * 40 + b" " + b"0" * 40 + b" D\tgone.py\n"
        b"\n"
        b"diff --git a/old.py b/new.py\nsimilarity index 91%\nrename from old.py\nrename to new.py\n"
        b"--- a/old.py\n+++ b/new.py\n@@ -2 +2,2 @@\n-x = 1\n+x = 2\n++++ not a header\n"
        b"diff --git a/fresh.py b/fresh.py\nnew file mode 100644\n--- /dev/null\n+++ b/fresh.py\n@@ -0,0 +1 @@\n+y = 1\n"
        b"\\ No newline at end of file\n"
        b"diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-z\n"
    )
    changes = parse_diff(output)
    assert set(changes) == {"new.py", "fresh.py"}
    assert changes["new.py"].old_path == "old.py"
    assert changes["new.py"].old_blob == "1" * 40
    assert changes["new.py"].added == b"x = 2\n+++ not a header\n"
    assert changes["new.py"].added_lines == 2
    assert changes["fresh.py"].old_blob is None
    assert changes["fresh.py"].added == b"y = 1\n"


def test_parse_blame_counts_lines_per_commit_and_path():
    sha_a, sha_b = "a" * 40, "b" * 40
    output = (
        f"{sha_a} 1 1 2\nauthor X\nfilename old name.py\n\tline one\n"
        f"{sha_a} 2 2\nauthor X\nfilename old name.py\n\tline two\n"
        f"{sha_b} 1 3 1\nauthor Y\nboundary\nfilename \"q\\\"uoted.py\"\n\t\n"
    ).encode()
    assert parse_blame(output) == {(sha_a, "old name.py"): 2, (sha_b, 'q"uoted.py'): 1}


def test_deleted_work_does_not_count(make_repo):
    repo = make_repo()
    first = repo.write("data.txt", LINES) or repo.commit("add data")
    extra = "".join(f"temporary_{i} = probe({i})\n" for i in range(20))
    repo.write("data.txt", LINES + extra)
    thrash = repo.commit("add temporary lines")
    repo.write("data.txt", LINES)
    repo.commit("remove them again")
    found, result = contributions(repo, "data.txt")
    assert set(found) == {first}
    assert found[first].surv == 1.0
    assert thrash not in found
    record = result.data["files"][0]
    assert record["commits"] == 1
    assert record["W"] == round(found[first].work, 4)


def test_partial_survival_scales_the_work(make_repo):
    repo = make_repo()
    repo.write("data.txt", LINES)
    first = repo.commit("add")
    repo.write("data.txt", "".join(LINES.splitlines(keepends=True)[:5]))
    repo.commit("trim to five lines")
    found, _ = contributions(repo, "data.txt")
    assert found[first].surviving_lines == 5
    assert found[first].surv == 0.25


def test_whitespace_only_commit_gets_no_credit_and_takes_none(make_repo):
    repo = make_repo()
    repo.write("data.txt", LINES)
    first = repo.commit("add")
    repo.write("data.txt", "".join("    " + line for line in LINES.splitlines(keepends=True)))
    reindent = repo.commit("reindent")
    found, _ = contributions(repo, "data.txt")
    assert set(found) == {first}
    assert reindent not in found
    assert found[first].surv == 1.0


def test_history_follows_renames(make_repo):
    repo = make_repo()
    repo.write("old/data.txt", LINES)
    first = repo.commit("add")
    repo.move("old/data.txt", "new/data.txt")
    repo.write("new/data.txt", LINES + "extra = 1\n")
    repo.commit("move and extend")
    found, _ = contributions(repo, "new/data.txt")
    assert found[first].path == "old/data.txt"
    assert found[first].surviving_lines == 20


def test_root_commit_is_compared_with_the_empty_tree(make_repo):
    repo = make_repo()
    repo.write("data.txt", LINES)
    root = repo.commit("root")
    found, _ = contributions(repo, "data.txt")
    assert found[root].added_lines == 20
    assert found[root].info > 0


def test_merges_are_credited_only_with_lines_new_to_every_parent(make_repo):
    repo = make_repo()
    repo.write("data.txt", "header = 1\nshared = 'base'\n")
    repo.commit("base")
    repo.checkout("feature", create=True)
    repo.write("data.txt", "header = 1\nshared = 'feature'\nfeature_only = True\n")
    feature = repo.commit("feature change")
    repo.checkout("main")
    repo.write("data.txt", "header = 1\nshared = 'main'\n")
    repo.commit("main change")
    merge = repo.merge("feature", "merge feature", resolve={
        "data.txt": "header = 1\nshared = 'resolved by hand'\nfeature_only = True\n",
    })
    found, _ = contributions(repo, "data.txt")
    assert [line for line, _ in found[merge].lines] == [2]  # the hand-resolved line only
    assert found[merge].added_lines == 1
    assert [line for line, _ in found[feature].lines] == [3]  # feature_only keeps its branch commit


def test_blame_ignore_revs_file_in_the_tree_is_honored(make_repo):
    repo = make_repo()
    repo.write("data.py", "".join(f"name_{i} = 'value {i}'\n" for i in range(10)))
    first = repo.commit("add")
    repo.write("data.py", "".join(f'name_{i} = "value {i}"\n' for i in range(10)))
    formatter = repo.commit("switch quote style")
    repo.write(".git-blame-ignore-revs", f"{formatter}\n")
    repo.commit("ignore the formatting commit")
    found, _ = contributions(repo, "data.py")
    assert formatter not in found
    assert found[first].surv == 1.0


def test_shallow_clone_nulls_w_unless_allowed(make_repo, tmp_path):
    repo = make_repo()
    repo.write("a.py", LINES)
    repo.commit("one")
    repo.write("a.py", LINES + "more = 1\n")
    repo.commit("two")
    clone = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{repo.path}", str(clone)], check=True)

    blocked = scan(clone).data
    record = blocked["files"][0]
    assert blocked["scan"]["shallow"] is True
    assert (record["W"], record["V"], record["reasons"]["W"], record["confidence"]) == (None, None, "shallow_clone", "partial")

    allowed = scan(clone, allow_shallow=True).data["files"][0]
    assert allowed["W"] > 0
    assert allowed["V"] is not None
    assert allowed["confidence"] == "partial"


def _branchy_history(repo):
    repo.write("a.py", LINES)
    repo.write("b.py", LINES.replace("value", "other"))
    repo.commit("one")
    repo.checkout("feature", create=True)
    repo.write("a.py", LINES + "feature_line = 1\n")
    repo.commit("feature")
    repo.checkout("main")
    repo.write("b.py", LINES.replace("value", "other") + "main_line = 2\n")
    repo.commit("main change")
    repo.merge("feature", "merge feature")
    repo.write("c.py", "late = 3\n")
    repo.commit("late file")


def test_blame_at_the_last_change_equals_blame_at_the_revision(make_repo):
    from vscore.adapters.history import BLAME_BASE, History, parse_blame_origins
    from vscore.gitrepo import BlobReader, Repo

    repo = make_repo()
    _branchy_history(repo)
    git = Repo(repo.path)
    head = git.resolve("HEAD")
    with BlobReader(git) as blobs:
        history = History(git, head, blobs, 9, None, copy_detection=2)
        history.blame_revs = history.last_changes(["a.py", "b.py", "c.py"])
        assert history.blame_revs["c.py"] == head and history.blame_revs["a.py"] != head  # a.py: the merge
        for path in ("a.py", "b.py", "c.py"):
            direct = parse_blame_origins(git.run(*BLAME_BASE, "-C", "-C", head, "--", path))
            assert history.blame(path) == direct


def test_a_new_commit_reblames_only_the_files_it_changed(make_repo, monkeypatch):
    from vscore.gitrepo import Repo

    repo = make_repo()
    _branchy_history(repo)
    before = scan(repo.path)
    repo.write("c.py", "late = 4\n")
    repo.commit("edit c")
    blamed = []
    run = Repo.run

    def spy(self, *args, **kwargs):
        if args[0] == "blame":
            blamed.append(args[-1])
        return run(self, *args, **kwargs)

    monkeypatch.setattr(Repo, "run", spy)
    after = scan(repo.path)
    assert blamed == ["c.py"]
    assert {f["path"]: f["W"] for f in after.data["files"] if f["path"] != "c.py"} == \
        {f["path"]: f["W"] for f in before.data["files"] if f["path"] != "c.py"}
