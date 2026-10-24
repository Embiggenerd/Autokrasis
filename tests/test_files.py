import pytest

from vscore.adapters.files import (
    CODE,
    CROWN_JEWEL,
    GENERATED,
    SCHEMA,
    SENSITIVE,
    TEST,
    classify,
    parse_crown_jewels,
    select,
)
from vscore.config import Config
from vscore.gitrepo import TreeEntry

CFG = Config()
GEN = {"linguist-generated": "set"}


def test_crown_jewels_file_is_a_yaml_list():
    text = "# protected\n- app/core.py\n- 'data/*.sql'  # all schema dumps\n\n"
    assert parse_crown_jewels(text) == ("app/core.py", "data/*.sql")
    with pytest.raises(ValueError):
        parse_crown_jewels("app/core.py\n")


@pytest.mark.parametrize(
    "path, attrs, expected",
    [
        ("app/core.py", {}, CODE),
        ("db/migrations/0001_initial.py", GEN, SCHEMA),  # generated migrations stay eligible
        ("alembic/versions/3f2a.py", {}, SCHEMA),
        ("schema.sql", {}, SCHEMA),
        ("auth/session_pb2.py", GEN, GENERATED),  # generated wins over sensitive
        ("app/models_pb2.py", {"linguist-vendored": "true"}, GENERATED),
        ("vendor/lib/x.py", {}, GENERATED),
        ("frontend/node_modules/a/index.js", {}, GENERATED),
        ("poetry.lock", {}, GENERATED),
        ("static/app.min.js", {}, GENERATED),
        ("app/billing/invoice.py", {}, SENSITIVE),
        ("app/user_service.py", {}, SENSITIVE),
        ("app/authority.py", {}, CODE),  # whole tokens only, not substrings
        ("tests/test_core.py", {}, TEST),
        ("app/core_test.py", {}, TEST),
        ("app/testing.py", {}, CODE),
        ("app/core.py", {"linguist-generated": "unset"}, CODE),
    ],
)
def test_classification_precedence(path, attrs, expected):
    assert classify(path, attrs, (), CFG)[0] == expected


def test_tests_are_recognized_before_sensitive_paths():
    # A test of auth code is still a test, so it never counts toward the auth module's D.
    assert classify("tests/test_auth.py", {}, (), CFG)[0] == TEST
    assert classify("app/auth/test_tokens.py", {}, (), CFG)[0] == TEST


def test_crown_jewel_wins_over_everything():
    assert classify("vendor/core.py", GEN, ("vendor/*",), CFG)[0] == CROWN_JEWEL


def test_select_reports_every_exclusion_reason():
    entries = [
        TreeEntry("app.py", "100644", "blob", "a" * 40, 10),
        TreeEntry("big.txt", "100644", "blob", "b" * 40, CFG.max_file_bytes + 1),
        TreeEntry("logo.png", "100644", "blob", "c" * 40, 10),
        TreeEntry("link", "120000", "blob", "d" * 40, 10),
        TreeEntry("lib", "160000", "commit", "e" * 40, None),
        TreeEntry("yarn.lock", "100644", "blob", "f" * 40, 10),
    ]
    eligible, excluded = select(entries, {}, (), CFG, is_binary=lambda e: e.path.endswith(".png"))
    assert [f.path for f in eligible] == ["app.py"]
    assert dict(excluded) == {
        "big.txt": "too_large",
        "logo.png": "binary",
        "link": "symlink",
        "lib": "submodule",
        "yarn.lock": "lockfile",
    }
