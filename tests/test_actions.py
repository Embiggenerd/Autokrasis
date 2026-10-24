import pytest

from vscore.actions import Context, classify_sql, from_tool_call, parse_shell, split_sql


@pytest.fixture
def ctx(tmp_path):
    return Context(
        repo_root=str(tmp_path),
        cwd=str(tmp_path),
        tracked={"app/core.py", "app/views.py", "app/legacy/a.py", "app/legacy/b.py", "README.md", "data/shop.sqlite"},
        tables={"customers": ["table:shop.main.customers"], "orders": ["table:shop.main.orders"],
                "cache": ["table:shop.main.cache"]},
        databases={str(tmp_path / "data" / "shop.sqlite"): "shop"},
    )


def effects(targets):
    return sorted((t.node, t.effect) for t in targets)


def test_split_sql_respects_quotes_and_comments():
    assert split_sql("DELETE FROM t WHERE a = ';'; -- note\nDROP TABLE x; /* c */") == [
        "DELETE FROM t WHERE a = ';'", "DROP TABLE x"]


@pytest.mark.parametrize("sql, expected", [
    ("DROP TABLE customers", [("table:shop.main.customers", "drop")]),
    ("drop table if exists main.orders, customers cascade",
     [("table:shop.main.customers", "drop"), ("table:shop.main.orders", "drop")]),
    ("TRUNCATE TABLE orders RESTART IDENTITY", [("table:shop.main.orders", "truncate")]),
    ("DELETE FROM customers", [("table:shop.main.customers", "bulk_delete")]),
    ("DELETE FROM customers WHERE 1=1", [("table:shop.main.customers", "bulk_delete")]),
    ("DELETE FROM cache WHERE created < '2024-01-01'", [("table:shop.main.cache", "partial_delete")]),
    ("UPDATE customers SET email = NULL", [("table:shop.main.customers", "bulk_update")]),
    ("UPDATE customers SET email = 'x' WHERE id = 3", []),
    ("ALTER TABLE customers DROP COLUMN email", [("table:shop.main.customers", "drop_column")]),
    ("ALTER TABLE customers DROP CONSTRAINT fk", []),
    ("ALTER TABLE customers RENAME TO clients", [("table:shop.main.customers", "rename_table")]),
    ("SELECT * FROM customers; INSERT INTO orders VALUES (1)", []),
    ("DROP TABLE invoices", [("table:?.?.invoices", "drop")]),  # unknown tables still count
])
def test_classify_sql(ctx, sql, expected):
    assert effects(classify_sql(sql, ctx)) == expected


def test_rm_paths_directories_and_globs(ctx):
    targets, _ = parse_shell("rm -rf app/legacy && rm README.md", ctx)
    assert effects(targets) == [("file:README.md", "delete"), ("file:app/legacy/a.py", "delete"),
                                ("file:app/legacy/b.py", "delete")]
    assert all(t.recursive for t in targets if "legacy" in t.node)
    globbed, _ = parse_shell("rm 'app/*.py'", ctx)
    assert effects(globbed) == [("file:app/core.py", "delete"), ("file:app/views.py", "delete")]


def test_removing_a_database_file_drops_every_table(ctx):
    targets, _ = parse_shell("rm data/shop.sqlite", ctx)
    assert {t.effect for t in targets} == {"db_reset"}
    assert len(targets) == 3


def test_sql_clients(ctx):
    targets, _ = parse_shell('sqlite3 data/shop.sqlite "DELETE FROM orders; DROP TABLE cache;"', ctx)
    assert effects(targets) == [("table:shop.main.cache", "drop"), ("table:shop.main.orders", "bulk_delete")]
    pg, _ = parse_shell("psql postgresql://localhost/shop -c 'TRUNCATE customers'", ctx)
    assert effects(pg) == [("table:shop.main.customers", "truncate")]
    heredoc, _ = parse_shell("sqlite3 data/shop.sqlite <<EOF\nDROP TABLE orders;\nEOF", ctx)
    assert effects(heredoc) == [("table:shop.main.orders", "drop")]
    interactive, notes = parse_shell("sqlite3 data/shop.sqlite", ctx)
    assert {t.effect for t in interactive} == {"unresolved"} and notes


def test_git_operations(ctx):
    assert effects(parse_shell("git push --force origin main", ctx)[0]) == [("git:history", "rewrite_history")]
    assert effects(parse_shell("git push origin +main", ctx)[0]) == [("git:history", "rewrite_history")]
    assert effects(parse_shell("git reset --hard HEAD~3", ctx)[0]) == [("git:worktree", "discard_uncommitted")]
    assert effects(parse_shell("git clean -fdx", ctx)[0]) == [("git:untracked", "discard_uncommitted")]
    assert effects(parse_shell("git checkout -- app/core.py", ctx)[0]) == [("git:worktree", "discard_uncommitted")]
    assert effects(parse_shell("git rm -r app/legacy", ctx)[0]) == [("file:app/legacy/a.py", "delete"),
                                                                    ("file:app/legacy/b.py", "delete")]
    assert parse_shell("git status && git diff && git commit -m 'rm -rf x'", ctx)[0] == []


def test_migration_resets_and_wrappers(ctx):
    targets, _ = parse_shell("python manage.py migrate shop zero", ctx)
    assert {t.effect for t in targets} == {"db_reset"} and len(targets) == 3
    nested, _ = parse_shell("bash -c 'rm -rf app/legacy'", ctx)
    assert len(nested) == 2
    assert parse_shell("sudo FOO=1 rm app/core.py", ctx)[0][0].node == "file:app/core.py"


def test_redirections_overwrite_tracked_files(ctx):
    assert effects(parse_shell("echo hi > README.md", ctx)[0]) == [("file:README.md", "overwrite")]
    assert parse_shell("pytest -q 2>&1 > /dev/null", ctx)[0] == []
    assert parse_shell("echo hi >> README.md", ctx)[0] == []  # appending destroys nothing


def test_unknown_commands_only_count_when_they_touch_priced_things(ctx):
    assert parse_shell("./build.sh --release", ctx)[0] == []
    targets, notes = parse_shell("./wipe.sh app/legacy", ctx)
    assert {t.effect for t in targets} == {"unresolved"} and notes
    assert parse_shell("pytest app && ls app", ctx)[0] == []


def test_mv_is_harmless_unless_it_overwrites(ctx):
    assert {t.effect for t in parse_shell("mv app/core.py app/core2.py", ctx)[0]} == {"move"}
    assert ("file:app/views.py", "overwrite") in effects(parse_shell("mv app/core.py app/views.py", ctx)[0])


def test_tool_calls(ctx, tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "core.py").write_text("".join(f"line {i}\n" for i in range(10)))
    rewrite = from_tool_call("Write", {"file_path": str(tmp_path / "app/core.py"), "content": "new\n"}, ctx)
    assert effects(rewrite.targets) == [("file:app/core.py", "rewrite")]
    small = from_tool_call("Edit", {"file_path": str(tmp_path / "app/core.py"), "old_string": "line 1", "new_string": "x"}, ctx)
    assert small.targets == ()
    assert from_tool_call("Read", {"file_path": "x"}, ctx).targets == ()
    bash = from_tool_call("Bash", {"command": "rm app/core.py"}, ctx)
    assert effects(bash.targets) == [("file:app/core.py", "delete")]
