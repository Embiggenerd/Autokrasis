from fixture_app import build

from vscore.baseline import churn, parse_numstat, spearman
from vscore.gitrepo import Repo
from vscore.harness.perturb import band_metrics, correlations, perturb, read_labels, write_labels


def test_spearman():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
    assert abs(spearman([1, 2, 2, 3], [1, 2, 3, 4]) - 0.9487) < 1e-4
    assert spearman([1, 1, 1], [1, 2, 3]) is None


def test_parse_numstat_reads_renames():
    output = b"3\t1\ta.py\x00-\t-\tlogo.png\x002\t0\t\x00old.py\x00new.py\x00"
    assert parse_numstat(output) == [(3, 1, None, "a.py"), (0, 0, None, "logo.png"), (2, 0, "old.py", "new.py")]


def test_churn_follows_renames(make_repo):
    repo = make_repo()
    repo.write("old.txt", "".join(f"line {i}\n" for i in range(30)))
    repo.commit("add")
    repo.move("old.txt", "new.txt")
    repo.write("new.txt", "".join(f"line {i}\n" for i in range(30)) + "tail\n")
    repo.commit("move")
    assert churn(Repo(repo.path), "HEAD", ["new.txt"]) == {"new.txt": 31}


def test_perturbation_check_on_the_fixture_app(tmp_path):
    build(tmp_path / "shop")
    cases, baseline = perturb(tmp_path / "shop")
    failing = {c.path: c.failing for c in cases}
    assert baseline == 16
    assert failing["shop/db.py"] == baseline  # conftest imports it, so every test breaks
    assert failing["shop/legacy.py"] == 0
    assert failing["shop/text.py"] == 2
    rho = correlations(cases)
    assert set(rho) == {"V", "W", "D", "price", "band", "default_band", "churn", "size"}
    assert band_metrics(cases)["band_accuracy"] is not None
    labels = tmp_path / "labels.csv"
    write_labels(cases, labels)
    assert len(read_labels(labels)) == len(cases)
