"""vscore compare: the W an action would destroy, compared with the W of the code the bug is about."""

import hashlib
import io
import json
import sqlite3
from contextlib import redirect_stdout

import pytest

from vscore import cli
from vscore.compare import compare, hook

LIMIT = 2.0


def code(tag: str, n: int) -> str:
    """n lines of distinct code, so each file carries real W."""
    return "".join(f"def {tag}_{i}():\n    return '{hashlib.sha256(f'{tag}{i}'.encode()).hexdigest()[:40]}'\n\n"
                   for i in range(n))


@pytest.fixture
def repo(make_repo, monkeypatch):
    r = make_repo()
    r.write("app/billing.py", code("bill", 80))
    r.write("app/users.py", code("user", 80))
    r.write("app/reports.py", code("report", 80))
    r.write("app/tiny.py", "X = 1\n")
    r.write(".gitignore", "*.sqlite\n")
    r.commit("app")
    db = sqlite3.connect(r.path / "shop.sqlite")
    db.executescript("CREATE TABLE orders (id INTEGER); INSERT INTO orders VALUES (1);")
    db.commit()
    db.close()
    monkeypatch.delenv("VSCORE_BUG", raising=False)
    return r


def run(repo, command, bug=("app/billing.py",)):
    return compare(repo.path, list(bug), "Bash", {"command": command})


def test_a_fix_inside_the_bug_runs(repo):
    result = run(repo, "rm app/billing.py")  # even replacing the whole buggy file is within the bug
    assert result.ratio <= 1.0 and result.allowed
    renamed = compare(repo.path, ["app/billing.py"], "Edit", {"file_path": str(repo.path / "app/billing.py"),
                                                             "old_string": "def bill_3(", "new_string": "def bill_x("})
    assert 0 < renamed.destroyed < renamed.bug_w  # a removed name is a (small) loss
    old = code("bill", 4).splitlines()[1]  # the body of bill_0: an edit that removes nothing
    edit = compare(repo.path, ["app/billing.py"], "Edit", {"file_path": str(repo.path / "app/billing.py"),
                                                          "old_string": old, "new_string": "    return 'fixed'"})
    assert edit.destroyed == 0 and edit.allowed


def test_erasing_the_codebase_stops(repo):
    result = run(repo, "rm -rf app")
    assert not result.allowed
    assert result.ratio > 2.5  # three files of similar size destroyed, against one
    assert result.destroyed == pytest.approx(result.codebase, rel=0.01)
    assert "app/users.py" in {f for f, _ in result.files}


def test_a_small_unrelated_deletion_runs(repo):
    assert run(repo, "rm app/tiny.py").allowed


def test_deleting_one_unrelated_module_of_the_same_size_runs_two_do_not(repo):
    assert run(repo, "rm app/users.py").allowed  # about 1x the bug
    assert not run(repo, "rm app/users.py app/reports.py app/billing.py").allowed


def test_what_cannot_be_weighed_against_code_stops(repo):
    for command in ("rm shop.sqlite", "sqlite3 shop.sqlite 'DROP TABLE orders'", "git push --force",
                    "cat files.txt | xargs rm"):
        result = run(repo, command)
        assert not result.allowed and result.unmeasured, command
    assert run(repo, "sqlite3 shop.sqlite 'DELETE FROM orders WHERE id = 1'").allowed  # a few rows


def test_without_a_bug_only_small_losses_run(repo):
    assert run(repo, "rm app/tiny.py", bug=()).allowed
    assert not run(repo, "rm app/users.py", bug=()).allowed


def test_the_cli_prints_both_numbers_and_the_ratio(repo):
    out = io.StringIO()
    with redirect_stdout(out):
        code_ = cli.main(["compare", str(repo.path), "--bug", "app/billing.py", "--command", "rm -rf app"])
    text = out.getvalue()
    assert code_ == 4 and "bug" in text and "destroys" in text and "STOP" in text
    out = io.StringIO()
    with redirect_stdout(out):
        assert cli.main(["compare", str(repo.path), "--bug", "app/billing.py", "--command", "ls", "--json"]) == 0
    assert json.loads(out.getvalue())["allowed"] is True


def test_the_hook_denies_with_the_numbers_and_stays_silent_otherwise(repo, monkeypatch):
    monkeypatch.setenv("VSCORE_BUG", "app/billing.py")
    payload = {"cwd": str(repo.path), "tool_name": "Bash"}
    assert hook({**payload, "tool_input": {"command": "rm app/billing.py"}}) is None
    answer = hook({**payload, "tool_input": {"command": "rm -rf app"}})["hookSpecificOutput"]
    assert answer["permissionDecision"] == "deny" and "the bug" in answer["permissionDecisionReason"]


def test_the_bug_can_come_from_the_report_file(repo, monkeypatch):
    (repo.path / ".vscore").mkdir()
    (repo.path / ".vscore" / "report.json").write_text(json.dumps({"nodes": ["file:app/billing.py"]}))
    assert hook({"cwd": str(repo.path), "tool_name": "Bash", "tool_input": {"command": "rm app/billing.py"}}) is None
