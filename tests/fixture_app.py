"""A small shop app with a realistic history, for checking V against tests broken by deleting a file.

Run `python tests/fixture_app.py <path>` to build it as a standalone repository.
"""

from __future__ import annotations

import sys

from gitfixture import GitFixture

DB = '''"""In-memory storage shared by every model."""

_tables = {}


def table(name):
    return _tables.setdefault(name, {})


def put(name, key, row):
    table(name)[key] = dict(row)
    return key


def get(name, key):
    row = table(name).get(key)
    return dict(row) if row is not None else None


def clear():
    _tables.clear()
'''

MODELS_V1 = '''from shop import db


def create_user(user_id, email):
    return db.put("users", user_id, {"id": user_id, "email": email})


def get_user(user_id):
    return db.get("users", user_id)


def create_order(order_id, user_id, items):
    if get_user(user_id) is None:
        raise KeyError(user_id)
    return db.put("orders", order_id, {"id": order_id, "user": user_id, "items": list(items)})


def get_order(order_id):
    return db.get("orders", order_id)
'''

MODELS_V2 = MODELS_V1.replace(
    '    return db.put("users", user_id, {"id": user_id, "email": email})',
    '    if "@" not in email:\n        raise ValueError("invalid email")\n'
    '    return db.put("users", user_id, {"id": user_id, "email": email.lower()})',
)

BILLING_V1 = '''from shop import models


def subtotal(order_id):
    order = models.get_order(order_id)
    result = 0
    for name, qty, price in order["items"]:
        result = result + qty * price
    return result


def total(order_id):
    return subtotal(order_id)
'''

BILLING_V2 = '''from shop import models

TAX_RATE = 0.2


def subtotal(order_id):
    order = models.get_order(order_id)
    return sum(qty * price for _, qty, price in order["items"])


def total(order_id):
    return round(subtotal(order_id) * (1 + TAX_RATE), 2)
'''

AUTH = '''import hashlib

from shop import db, models


def _digest(salt, password):
    return hashlib.sha256((salt + password).encode()).hexdigest()


def set_password(user_id, password):
    salt = hashlib.sha256(user_id.encode()).hexdigest()[:8]
    db.put("passwords", user_id, {"salt": salt, "digest": _digest(salt, password)})


def login(user_id, password):
    if models.get_user(user_id) is None:
        return False
    record = db.get("passwords", user_id)
    return record is not None and _digest(record["salt"], password) == record["digest"]
'''

API = '''from shop import auth, billing, models


def checkout(user_id, password, order_id):
    if not auth.login(user_id, password):
        return {"status": 401}
    order = models.get_order(order_id)
    if order is None or order["user"] != user_id:
        return {"status": 404}
    return {"status": 200, "total": billing.total(order_id)}
'''

REPORTS = '''from shop import billing


def revenue(order_ids):
    return round(sum(billing.total(o) for o in order_ids), 2)
'''

TEXT = '''import re


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
'''

LEGACY = '''def old_discount(amount):
    return amount * 0.9
'''

CONFTEST = '''import pytest

from shop import db


@pytest.fixture(autouse=True)
def fresh_db():
    db.clear()
    yield
    db.clear()
'''

TEST_DB = '''from shop import db


def test_put_and_get():
    db.put("t", 1, {"a": 1})
    assert db.get("t", 1) == {"a": 1}


def test_missing_row():
    assert db.get("t", 2) is None
'''

TEST_MODELS = '''import pytest

from shop import models


def test_user_round_trip():
    models.create_user("u1", "A@x.com")
    assert models.get_user("u1")["email"] == "a@x.com"


def test_rejects_bad_email():
    with pytest.raises(ValueError):
        models.create_user("u1", "nope")


def test_order_needs_user():
    with pytest.raises(KeyError):
        models.create_order("o1", "ghost", [])
'''

TEST_INVOICES = '''from shop import billing, models


def setup_order():
    models.create_user("u1", "a@x.com")
    models.create_order("o1", "u1", [("pen", 2, 1.5), ("pad", 1, 4.0)])


def test_subtotal():
    setup_order()
    assert billing.subtotal("o1") == 7.0


def test_total_includes_tax():
    setup_order()
    assert billing.total("o1") == 8.4
'''

TEST_LOGIN = '''from shop import auth, models


def test_login_succeeds_with_right_password():
    models.create_user("u1", "a@x.com")
    auth.set_password("u1", "secret")
    assert auth.login("u1", "secret")


def test_login_fails_with_wrong_password():
    models.create_user("u1", "a@x.com")
    auth.set_password("u1", "secret")
    assert not auth.login("u1", "guess")


def test_login_fails_for_unknown_user():
    assert not auth.login("ghost", "secret")
'''

TEST_API = '''from shop import api, auth, models


def setup():
    models.create_user("u1", "a@x.com")
    auth.set_password("u1", "secret")
    models.create_order("o1", "u1", [("pen", 2, 1.5)])


def test_checkout_ok():
    setup()
    assert api.checkout("u1", "secret", "o1") == {"status": 200, "total": 3.6}


def test_checkout_wrong_password():
    setup()
    assert api.checkout("u1", "nope", "o1")["status"] == 401


def test_checkout_unknown_order():
    setup()
    assert api.checkout("u1", "secret", "o9")["status"] == 404
'''

TEST_REPORTS = '''from shop import models, reports


def test_revenue():
    models.create_user("u1", "a@x.com")
    models.create_order("o1", "u1", [("pen", 1, 10.0)])
    models.create_order("o2", "u1", [("pad", 1, 5.0)])
    assert reports.revenue(["o1", "o2"]) == 18.0
'''

TEST_TEXT = '''from shop import text


def test_slugify():
    assert text.slugify("Hello, World!") == "hello-world"


def test_slugify_empty():
    assert text.slugify("") == ""
'''


def build(path) -> GitFixture:
    repo = GitFixture(path)
    repo.write("shop/__init__.py", "")
    repo.write("shop/db.py", DB)
    repo.write("tests/conftest.py", CONFTEST)
    repo.write("tests/test_db.py", TEST_DB)
    repo.commit("storage")
    repo.write("shop/models.py", MODELS_V1)
    repo.write("tests/test_models.py", TEST_MODELS.replace('"a@x.com"', '"A@x.com"').split("\n\n\ndef test_rejects")[0])
    repo.commit("users and orders")
    repo.write("shop/billing.py", BILLING_V1)
    repo.commit("billing")
    repo.write("shop/auth.py", AUTH)
    repo.write("tests/test_login.py", TEST_LOGIN)
    repo.commit("passwords and login")
    repo.write("shop/billing.py", BILLING_V2)
    repo.write("tests/test_invoices.py", TEST_INVOICES)
    repo.commit("tax on totals")
    repo.write("shop/api.py", API)
    repo.write("tests/test_api.py", TEST_API)
    repo.commit("checkout endpoint")
    repo.write("shop/reports.py", REPORTS)
    repo.write("tests/test_reports.py", TEST_REPORTS)
    repo.commit("revenue report")
    repo.write("shop/utils.py", TEXT)
    repo.write("shop/legacy.py", LEGACY)
    repo.write("tests/test_text.py", TEST_TEXT.replace("import text", "import utils as text"))
    repo.commit("helpers")
    repo.write("shop/models.py", MODELS_V2)
    repo.write("tests/test_models.py", TEST_MODELS)
    repo.commit("validate and normalize email")
    repo.write("shop/api.py", API.replace("    ", "\t").replace("\t", "    "))
    repo.write("shop/reports.py", REPORTS + "\n")
    repo.commit("whitespace cleanup")
    repo.move("shop/utils.py", "shop/text.py")
    repo.write("tests/test_text.py", TEST_TEXT)
    repo.commit("rename utils to text")
    return repo


if __name__ == "__main__":
    build(sys.argv[1])
    print(f"built fixture app at {sys.argv[1]}")
