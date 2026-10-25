import json

from vscore.cli import main
from vscore.scan import scan


def small_app(repo):
    repo.write("app/__init__.py", "")
    repo.write("app/core.py", "def area(w, h):\n    return w * h\n")
    repo.write("app/views.py", "from app import core\n\ndef page():\n    return core.area(2, 3)\n")
    repo.write("tests/test_core.py", "from app import core\n\ndef test_area():\n    assert core.area(2, 3) == 6\n")
    repo.write("deploy.sh", "#!/bin/sh\necho deploying\n")
    repo.write("db/migrations/0001_init.py", "OPERATIONS = ['create table users']\n")
    repo.write(".gitattributes", "db/migrations/*.py linguist-generated\ngen/*.py linguist-generated\n")
    repo.write("gen/client_pb2.py", "CLIENT = 1\n")
    repo.write("crown_jewels.yml", "- app/views.py\n")
    repo.write("broken.py", "def (:\n")
    repo.commit("app")


def by_path(data):
    return {f["path"]: f for f in data["files"]}


def test_scan_output_follows_the_spec(make_repo):
    repo = make_repo()
    small_app(repo)
    data = scan(repo.path).data
    files = by_path(data)

    assert files["app/core.py"]["class"] == "code"
    assert files["app/core.py"]["D"] == 1  # views imports it; the test file does not count
    assert files["app/views.py"]["class"] == "crown_jewel"
    assert files["db/migrations/0001_init.py"]["class"] == "schema"
    assert {"path": "gen/client_pb2.py", "reason": "linguist_generated"} in data["excluded"]
    assert files["tests/test_core.py"]["class"] == "test"

    script = files["deploy.sh"]
    assert script["W"] > 0
    assert (script["D"], script["V"], script["reasons"]) == (None, None, {"D": "unsupported_language"})

    broken = files["broken.py"]
    assert (broken["D"], broken["V"], broken["reasons"]) == (None, None, {"D": "parse_error"})

    core = files["app/core.py"]
    assert core["confidence"] == "full" and core["V"] is not None and core["commits"] == 1

    total_w = sum(f["W"] for f in data["files"] if f["W"] is not None)
    assert abs(data["total"]["W"] - total_w) < 0.01
    scan_meta = data["scan"]
    assert scan_meta["formula"] == "vscore-formula/5"
    assert scan_meta["zstd"]["level"] == 9
    assert scan_meta["coverage"]["parse_errors"] == 1


def test_same_inputs_give_byte_identical_json(make_repo):
    repo = make_repo()
    small_app(repo)
    repo.write("app/core.py", "def area(w, h):\n    return w * h\n\ndef perimeter(w, h):\n    return 2 * (w + h)\n")
    repo.commit("perimeter")
    first = scan(repo.path, jobs=1).to_json()
    second = scan(repo.path, jobs=8).to_json()  # second run also reads the blame cache
    assert first == second


def test_older_revision_can_be_scanned(make_repo):
    repo = make_repo()
    small_app(repo)
    old = repo.git("rev-parse", "HEAD")
    repo.write("app/extra.py", "X = 1\n")
    repo.commit("extra")
    data = scan(repo.path, rev=old).data
    assert data["scan"]["revision"] == old
    assert "app/extra.py" not in by_path(data)


def test_cli_scan_show_and_explain(make_repo, tmp_path, capsys):
    repo = make_repo()
    small_app(repo)
    out = tmp_path / "scan.json"
    assert main(["scan", str(repo.path), "-o", str(out)]) == 0
    assert json.loads(out.read_text())["files"]
    assert main(["show", str(out), "--top", "3"]) == 0
    assert main(["explain", str(repo.path), "app/core.py"]) == 0
    printed = capsys.readouterr().out
    assert "app/core.py" in printed and "imported directly by 2" in printed  # views and the test file
    assert main(["explain", str(repo.path), "gen/client_pb2.py"]) == 1
