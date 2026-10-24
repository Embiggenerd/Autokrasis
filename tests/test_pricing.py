import math
import sqlite3

import pytest

from vscore.adapters.schema import read_tables
from vscore.config import Config
from vscore.evidence import Evidence
from vscore.prices.model import (
    band,
    calibration_predict,
    combine,
    default_file_band,
    fit_calibration,
    price_file,
    price_table,
)
from vscore.prices.run import price_repo

CFG = Config()
NOW = "2024-06-01T00:00:00+00:00"


@pytest.mark.parametrize("hours, expected", [(0.2, 0), (1, 1), (9.9, 1), (10, 2), (150, 3), (1000, 4), (5000, 4)])
def test_bands(hours, expected):
    assert band(hours) == expected


@pytest.mark.parametrize(
    "cls, path, d, expected",
    [
        ("crown_jewel", "a.py", 0, 4),
        ("schema", "db/migrations/1.py", 0, 3),
        ("sensitive", "auth.py", 0, 3),
        ("test", "tests/test_a.py", 4, 0),
        ("code", "README.md", None, 0),
        ("code", "lib.py", 3, 2),
        ("code", "leaf.ts", 0, 1),
        ("code", "deploy.sh", None, 2),
        ("code", ".github/workflows/ci.yml", None, 2),
        ("code", "docs/Makefile", None, 2),
        ("code", "static/logo.svg", None, 1),
        ("code", "data/export.parquet", None, 3),  # unknown is expensive
    ],
)
def test_default_bands(cls, path, d, expected):
    assert default_file_band(cls, path, d, CFG) == expected


def test_history_raises_freely_but_lowers_only_with_confidence():
    assert combine(1, 3, "code", confident=False) == 3
    assert combine(2, 0, "code", confident=False) == 2
    assert combine(2, 0, "code", confident=True) == 0
    assert combine(3, 0, "sensitive", confident=True) == 2  # at most one band
    assert combine(4, 0, "crown_jewel", confident=True) == 4
    assert combine(3, None, "code", confident=True) == 3


def test_committed_code_price_is_mostly_harm():
    price = price_file("lib.py", "code", 5000.0, 4, "full", [("c1", 5000.0)], Evidence(), CFG)
    assert price.parts["rebuild"] == 10.0  # 5000 bytes / 500 per hour
    assert price.parts["harm"] == 8.0  # 4 importers * 2 h
    assert price.hours == pytest.approx(0.25 + 8.0)
    assert price.at_risk_hours == pytest.approx(18.0)
    assert price.band == 1 and price.at_risk_band == 2


def test_unverified_work_counts_less_and_unknown_counts_fully():
    evidence = Evidence(verified_by={"v": ["ci:1"]}, windows={"ci": ("2024-01-01", "2024-12-31")},
                        commit_dates={"v": "2024-03-01", "u": "2024-03-02", "old": "2023-01-01"})
    price = price_file("lib.py", "code", 3000.0, 0, "full", [("v", 1000.0), ("u", 1000.0), ("old", 1000.0)],
                       evidence, CFG)
    assert price.parts["W_verified"] == 2500.0
    assert "1 verified, 1 unverified, 1 unknown" in price.evidence[0]


def test_missing_w_keeps_the_default_band():
    price = price_file("data/export.parquet", "code", None, None, "none", None, Evidence(), CFG)
    assert (price.hours, price.band, price.default_band) == (None, 3, 3)


def test_incidents_and_traffic_raise_harm():
    evidence = Evidence(incidents={"api.py": [1]}, traffic={"api.py": 3000})
    price = price_file("api.py", "code", 100.0, 1, "full", [("c", 100.0)], evidence, CFG)
    assert price.parts["harm"] == pytest.approx(1 * 2.0 * (1 + math.log2(4)) + 40.0)
    assert price.band == 2


def make_db(path):
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT);"
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id));"
        "CREATE TABLE cache_pages (k TEXT);"
        "CREATE TABLE audit (id INTEGER);"
        "INSERT INTO customers VALUES (1, 'a@x.com'), (2, 'b@x.com');"
        "INSERT INTO orders VALUES (1, 1);"
        "INSERT INTO cache_pages VALUES ('x');"
    )
    db.commit()
    db.close()


def test_sqlite_tables_are_read_without_rows(tmp_path):
    make_db(tmp_path / "shop.sqlite")
    tables = {t.name: t for t in read_tables(f"sqlite:///{tmp_path / 'shop.sqlite'}", CFG.personal_columns)}
    assert tables["customers"].rows == 2
    assert tables["customers"].references_in == 1
    assert tables["customers"].personal_columns == ("email",)
    assert tables["orders"].references_out == 1
    assert tables["customers"].node == "table:shop.main.customers"


def test_table_prices(tmp_path):
    make_db(tmp_path / "shop.sqlite")
    tables = {t.name: t for t in read_tables(str(tmp_path / "shop.sqlite"), CFG.personal_columns)}
    customers = price_table(tables["customers"], Evidence(), CFG, NOW)
    assert customers.band == 4 and "no recent tested restore" in customers.evidence
    tested = Evidence(restore_tests={tables["customers"].node: "2024-05-30T00:00:00+00:00"})
    assert price_table(tables["customers"], tested, CFG, NOW).band == 2  # restorable, but personal data
    stale = Evidence(restore_tests={tables["customers"].node: "2024-01-01T00:00:00+00:00"})
    assert price_table(tables["customers"], stale, CFG, NOW).band == 4
    assert price_table(tables["cache_pages"], Evidence(), CFG, NOW).band == 0
    assert price_table(tables["audit"], Evidence(), CFG, NOW).band == 0  # empty: schema only


def test_calibration_recovers_a_known_relationship():
    rows = [(w, d, math.exp(0.5 + 0.2 * math.log2(1 + w) + 0.7 * math.log2(1 + d)))
            for w, d in [(100, 0), (500, 1), (2000, 3), (50, 2), (8000, 0), (300, 5), (1200, 1)]]
    fit = fit_calibration(rows)
    assert fit["b1"] == pytest.approx(0.2, abs=1e-6) and fit["b2"] == pytest.approx(0.7, abs=1e-6)
    assert fit["r2"] == pytest.approx(1.0)
    assert calibration_predict(fit, 2000, 3) == pytest.approx(rows[2][2], rel=1e-6)
    with pytest.raises(ValueError):
        fit_calibration(rows[:3])


def test_price_repo_end_to_end(make_repo, tmp_path):
    repo = make_repo()
    repo.write("core.py", "def f():\n    return 1\n")
    repo.write("app.py", "import core\n")
    repo.write("README.md", "# hi\n")
    repo.commit("init")
    make_db(tmp_path / "shop.sqlite")
    result = price_repo(repo.path, databases=(f"sqlite:///{tmp_path / 'shop.sqlite'}",), now=NOW)
    nodes = {n["node"]: n for n in result.data["nodes"]}
    assert nodes["file:core.py"]["parts"]["harm"] == 2.0
    assert nodes["file:README.md"]["band"] == 0
    assert nodes["table:shop.main.customers"]["band"] == 4
    assert result.data["prices"]["events"]["git"] == 3
    again = price_repo(repo.path, databases=(f"sqlite:///{tmp_path / 'shop.sqlite'}",), now=NOW)
    assert again.to_json() == result.to_json()  # re-ingesting history changes nothing
